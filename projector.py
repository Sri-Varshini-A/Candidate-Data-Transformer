"""
projector.py — Stage 4: Configurable Projection

Reshapes a canonical CandidateProfile into the exact output schema the
caller wants via a runtime JSON config — without changing the engine.

Clean separation: this module only READS from CandidateProfile; it never
mutates it, so the same canonical record can be projected multiple times
with different configs.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from models import CandidateProfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config schema (validated with Pydantic)
# ---------------------------------------------------------------------------

class FieldSpec(BaseModel):
    path:      str                    # output key name in the projected dict
    from_:     Optional[str] = Field(None, alias="from")   # source JSONPath e.g. "emails[0]"
    type:      str = "string"         # "string" | "string[]" | "number" | "boolean" | "object" | "object[]"
    required:  bool = False
    normalize: Optional[str] = None  # "E164" | "canonical"

    model_config = {"populate_by_name": True}

    @field_validator("type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        allowed = {"string", "string[]", "number", "boolean", "object", "object[]"}
        if v not in allowed:
            raise ValueError(f"type must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("normalize")
    @classmethod
    def _valid_normalize(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        allowed = {"E164", "canonical"}
        if v not in allowed:
            raise ValueError(f"normalize must be one of {sorted(allowed)}, got {v!r}")
        return v


class ProjectionConfig(BaseModel):
    fields:              list[FieldSpec]
    include_confidence:  bool = True
    include_provenance:  bool = True
    on_missing:          str  = "null"   # "null" | "omit" | "error"

    @field_validator("on_missing")
    @classmethod
    def _valid_on_missing(cls, v: str) -> str:
        if v not in ("null", "omit", "error"):
            raise ValueError(f"on_missing must be 'null', 'omit', or 'error', got {v!r}")
        return v


class ProjectionError(RuntimeError):
    """Raised when on_missing='error' and a required field resolves to None."""


# ---------------------------------------------------------------------------
# JSONPath-lite resolver
# ---------------------------------------------------------------------------

# Supported patterns:
#   "full_name"          → profile.full_name
#   "emails[0]"          → profile.emails[0]
#   "skills[].name"      → [s.name for s in profile.skills]
#   "location.city"      → profile.location.city
#   "links.github"       → profile.links.github
#   "experience[0].company" → profile.experience[0].company

_ARRAY_IDX_RE = re.compile(r"^(\w+)\[(\d+)\]$")       # field[N]
_ARRAY_ALL_RE = re.compile(r"^(\w+)\[\]\.(\w+)$")     # field[].subfield


def resolve_path(profile: CandidateProfile, path: str) -> Any:
    """
    Resolve a JSONPath-lite expression against a CandidateProfile.
    Returns None on any IndexError / AttributeError — never raises.
    """
    if not path:
        return None

    # Normalise: use the profile's dict representation for generic traversal.
    # We build it lazily (only when needed) by converting to dict once.
    try:
        profile_dict = profile.model_dump()
    except Exception:
        return None

    # ── field[].subfield  (list comprehension) ────────────────────────────
    m = _ARRAY_ALL_RE.match(path)
    if m:
        field_name, sub = m.groups()
        lst = profile_dict.get(field_name, [])
        if not isinstance(lst, list):
            return None
        return [item.get(sub) for item in lst if isinstance(item, dict) and item.get(sub) is not None]

    # ── field[N]  (indexed access) ────────────────────────────────────────
    m = _ARRAY_IDX_RE.match(path)
    if m:
        field_name, idx_str = m.groups()
        lst = profile_dict.get(field_name, [])
        if not isinstance(lst, list):
            return None
        try:
            return lst[int(idx_str)]
        except IndexError:
            return None

    # ── dotted path  (nested object) ─────────────────────────────────────
    segments = path.split(".")
    node: Any = profile_dict
    for seg in segments:
        # Each segment may itself be an indexed access: "experience[0]"
        m_idx = _ARRAY_IDX_RE.match(seg)
        if m_idx:
            fname, idx_str = m_idx.groups()
            node = node.get(fname) if isinstance(node, dict) else None
            if isinstance(node, list):
                try:
                    node = node[int(idx_str)]
                except IndexError:
                    return None
            else:
                return None
        elif isinstance(node, dict):
            node = node.get(seg)
        else:
            return None
        if node is None:
            return None

    return node


# ---------------------------------------------------------------------------
# Post-resolution normalization
# ---------------------------------------------------------------------------

def _post_normalize(value: Any, normalize: Optional[str]) -> Any:
    """Apply a post-resolution normalization hint to a resolved value."""
    if normalize is None or value is None:
        return value

    if normalize == "E164":
        from normalizers import normalize_phone
        if isinstance(value, list):
            return [normalize_phone(v) or v for v in value]
        return normalize_phone(str(value)) or value

    if normalize == "canonical":
        from normalizers import canonicalize_skill
        if isinstance(value, list):
            return [canonicalize_skill(str(v)) for v in value]
        return canonicalize_skill(str(value))

    return value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_projection_config(config_path: Optional[str]) -> ProjectionConfig:
    """
    Load and validate a ProjectionConfig from a JSON file.
    Falls back to a safe default (all fields, provenance included) if the
    path is None or the file is missing / malformed.
    """
    if config_path:
        path = Path(config_path)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                return ProjectionConfig.model_validate(raw)
            except Exception as exc:
                logger.warning(
                    "Could not load projection config from '%s': %s. "
                    "Falling back to default (all fields).",
                    config_path, exc,
                )
        else:
            logger.warning("Projection config not found: %s. Using default.", config_path)

    return _default_config()


def project(
    profile: CandidateProfile,
    config: ProjectionConfig,
) -> dict:
    """
    Reshape a CandidateProfile according to the ProjectionConfig.

    Steps:
      1. For each FieldSpec, resolve the source path (from_ or path) via resolve_path().
      2. Apply post-normalization if specified.
      3. Handle None values via on_missing policy.
      4. Optionally append overall_confidence and provenance.

    Returns a plain dict ready for JSON serialization.
    Never raises unless on_missing='error' and a required field is missing.
    """
    output: dict = {}

    for spec in config.fields:
        source_path = spec.from_ or spec.path
        value = resolve_path(profile, source_path)
        value = _post_normalize(value, spec.normalize)

        if value is None or value == [] or value == "":
            # Apply on_missing policy
            if config.on_missing == "omit":
                continue
            if config.on_missing == "error" and spec.required:
                raise ProjectionError(
                    f"Required field '{spec.path}' (from '{source_path}') "
                    "resolved to None and on_missing='error'."
                )
            output[spec.path] = None
        else:
            output[spec.path] = value

    if config.include_confidence:
        output["overall_confidence"] = profile.overall_confidence

    if config.include_provenance:
        output["provenance"] = [p.model_dump() for p in profile.provenance]

    return output


# ---------------------------------------------------------------------------
# Default config — emits the full canonical schema
# ---------------------------------------------------------------------------

def _default_config() -> ProjectionConfig:
    return ProjectionConfig(
        fields=[
            FieldSpec(path="candidate_id"),
            FieldSpec(path="full_name",        required=True),
            FieldSpec(path="emails",            type="string[]"),
            FieldSpec(path="phones",            type="string[]"),
            FieldSpec(path="location",          type="object"),
            FieldSpec(path="links",             type="object"),
            FieldSpec(path="headline"),
            FieldSpec(path="years_experience",  type="number"),
            FieldSpec(path="skills",            type="object[]"),
            FieldSpec(path="experience",        type="object[]"),
            FieldSpec(path="education",         type="object[]"),
        ],
        include_confidence=True,
        include_provenance=True,
        on_missing="null",
    )
