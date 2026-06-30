"""
models.py — Data models for the Multi-Source Candidate Data Transformer.

Two tiers:
  1. RawCandidate  — lightweight dataclass; Stage 1 transport object (no validation).
  2. CandidateProfile + sub-models — Pydantic v2 canonical schema; produced by Stage 3.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Stage 1 transport object — intentionally unvalidated (raw data is messy)
# ---------------------------------------------------------------------------

@dataclass
class RawCandidate:
    """Carries raw, unvalidated data from one extraction source."""
    source: str               # "csv" | "github" | "resume"
    data: dict                # raw extracted fields, keys vary by source
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when data was extracted without errors."""
        return bool(self.data) and not self.errors

    @property
    def partial(self) -> bool:
        """True when some data was extracted but errors also occurred."""
        return bool(self.data) and bool(self.errors)


# ---------------------------------------------------------------------------
# Canonical profile sub-models (Pydantic v2)
# ---------------------------------------------------------------------------

class Location(BaseModel):
    city:    Optional[str] = None
    region:  Optional[str] = None
    country: Optional[str] = None   # ISO-3166 alpha-2 e.g. "US", "CN"

    @field_validator("country", mode="before")
    @classmethod
    def _validate_country(cls, v: object) -> Optional[str]:
        if v is None or v == "":
            return None
        s = str(v).strip().upper()
        if len(s) != 2 or not s.isalpha():
            return None   # invalid → null, never invented
        return s


class Links(BaseModel):
    linkedin:  Optional[str] = None
    github:    Optional[str] = None
    portfolio: Optional[str] = None
    other:     list[str] = Field(default_factory=list)


class Skill(BaseModel):
    name:       str
    confidence: float = Field(ge=0.0, le=1.0)
    sources:    list[str] = Field(default_factory=list)


class Experience(BaseModel):
    company: Optional[str] = None
    title:   Optional[str] = None
    start:   Optional[str] = None   # YYYY-MM
    end:     Optional[str] = None   # YYYY-MM or "present"
    summary: Optional[str] = None


class Education(BaseModel):
    institution: Optional[str] = None
    degree:      Optional[str] = None
    field:       Optional[str] = None
    end_year:    Optional[int] = None


class ProvenanceEntry(BaseModel):
    field:  str    # canonical profile field name
    source: str    # "csv" | "github" | "resume"
    method: str    # "direct" | "normalized" | "merged_highest_confidence" | "merged_union" | "parse_failed"


# ---------------------------------------------------------------------------
# Top-level canonical profile
# ---------------------------------------------------------------------------

class CandidateProfile(BaseModel):
    candidate_id:       str  = Field(default_factory=lambda: str(uuid.uuid4()))
    full_name:          Optional[str] = None
    emails:             list[str] = Field(default_factory=list)
    phones:             list[str] = Field(default_factory=list)    # E.164
    location:           Location  = Field(default_factory=Location)
    links:              Links     = Field(default_factory=Links)
    headline:           Optional[str]   = None
    years_experience:   Optional[float] = None
    skills:             list[Skill]     = Field(default_factory=list)
    experience:         list[Experience]= Field(default_factory=list)
    education:          list[Education] = Field(default_factory=list)
    provenance:         list[ProvenanceEntry] = Field(default_factory=list)
    overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("phones", mode="before")
    @classmethod
    def _dedupe_phones(cls, v: object) -> list[str]:
        if not isinstance(v, list):
            return []
        seen: set[str] = set()
        out: list[str] = []
        for p in v:
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        return out

    @field_validator("emails", mode="before")
    @classmethod
    def _dedupe_emails(cls, v: object) -> list[str]:
        if not isinstance(v, list):
            return []
        seen: set[str] = set()
        out: list[str] = []
        for e in v:
            e = (e or "").lower().strip()
            if e and e not in seen:
                seen.add(e)
                out.append(e)
        return out

    @field_validator("overall_confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v: object) -> float:
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, f))
