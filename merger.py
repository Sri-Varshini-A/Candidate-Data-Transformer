"""
merger.py — Stage 3: Sequential Merging

Takes a list of normalized source dicts (one per source per candidate) and
produces a single CandidateProfile with:
  - Deterministic conflict resolution via confidence weights
  - Provenance tracking for every field
  - Union-based deduplication for list fields (emails, phones, skills)
  - An overall_confidence score
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

from extractors import SOURCE_CONFIDENCE, SOURCE_PRIORITY
from models import (
    CandidateProfile,
    Education,
    Experience,
    Links,
    Location,
    ProvenanceEntry,
    Skill,
)
from normalizers import canonicalize_skill, classify_url

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _confidence(source: str, field: str) -> float:
    """Return the configured confidence weight for (source, field)."""
    return SOURCE_CONFIDENCE.get(source, {}).get(field, 0.3)


def _resolve_scalar(
    field: str,
    candidates: list[tuple[str, Any]],  # [(source, value), ...]
) -> tuple[Any, str, float]:
    """
    Pick the winning value for a scalar field.

    Algorithm:
      1. Filter out empty / None values.
      2. Score each (source, value) by SOURCE_CONFIDENCE[source][field].
      3. Among ties, apply SOURCE_PRIORITY order.
      4. Return (winning_value, winning_source, confidence).
    """
    valid = [(src, val) for src, val in candidates if val]
    if not valid:
        return None, "none", 0.0

    def sort_key(pair: tuple[str, Any]) -> tuple[float, int]:
        src, _ = pair
        conf = _confidence(src, field)
        priority = SOURCE_PRIORITY.index(src) if src in SOURCE_PRIORITY else len(SOURCE_PRIORITY)
        return (-conf, priority)  # descending confidence, ascending priority index

    valid.sort(key=sort_key)
    winner_src, winner_val = valid[0]
    return winner_val, winner_src, _confidence(winner_src, field)


def _stable_candidate_id(emails: list[str], github_url: Optional[str]) -> str:
    """
    Derive a deterministic candidate ID from their deduplication keys.
    Same email → same ID across pipeline runs.
    Falls back to a hash of the GitHub URL, then to a random UUID-like hash.
    """
    key = (emails[0] if emails else "") or (github_url or "") or "unknown"
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return f"cand_{digest}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def merge(
    normalized_sources: list[tuple[str, dict]],
) -> CandidateProfile:
    """
    Merge normalized source dicts into one CandidateProfile.

    Args:
        normalized_sources: [(source_name, normalized_dict), ...]
          where source_name ∈ {"csv", "github", "resume"}.
          Pass in SOURCE_PRIORITY order for determinism: csv first, then resume, then github.

    Returns:
        A fully populated CandidateProfile. All fields that couldn't be
        resolved are None / empty — never invented.
    """
    if not normalized_sources:
        return CandidateProfile()

    provenance: list[ProvenanceEntry] = []
    confidence_scores: list[float] = []

    def record(field: str, source: str, method: str, conf: float) -> None:
        provenance.append(ProvenanceEntry(field=field, source=source, method=method))
        if conf > 0:
            confidence_scores.append(conf)

    # ── full_name ────────────────────────────────────────────────────────────
    name_candidates = [
        (src, d.get("name", ""))
        for src, d in normalized_sources
    ]
    full_name, name_src, name_conf = _resolve_scalar("name", name_candidates)
    if full_name:
        record("full_name", name_src, "merged_highest_confidence", name_conf)

    # ── emails (union) ───────────────────────────────────────────────────────
    seen_emails: set[str] = set()
    emails: list[str] = []
    for src, d in normalized_sources:
        for raw_email in _iter_str_field(d, ["email"]):
            e = raw_email.lower().strip()
            if e and e not in seen_emails:
                seen_emails.add(e)
                emails.append(e)
                record("emails", src, "merged_union", _confidence(src, "email"))

    # ── phones (union, E.164) ────────────────────────────────────────────────
    seen_phones: set[str] = set()
    phones: list[str] = []
    for src, d in normalized_sources:
        for raw_phone in _iter_str_field(d, ["phone", "mobile_number"]):
            if raw_phone and raw_phone not in seen_phones:
                seen_phones.add(raw_phone)
                phones.append(raw_phone)
                record("phones", src, "merged_union", _confidence(src, "phone"))

    # ── location ─────────────────────────────────────────────────────────────
    loc_candidates = [(src, d.get("location", "")) for src, d in normalized_sources]
    raw_loc, loc_src, loc_conf = _resolve_scalar("location", loc_candidates)

    country_candidates = [(src, d.get("country")) for src, d in normalized_sources]
    country, country_src, country_conf = _resolve_scalar("location", country_candidates)

    location = Location(
        city=_extract_city(raw_loc),
        region=None,
        country=country,
    )
    if raw_loc or country:
        record("location", loc_src or country_src, "merged_highest_confidence",
               max(loc_conf, country_conf))

    # ── links ────────────────────────────────────────────────────────────────
    github_url: Optional[str] = None
    linkedin_url: Optional[str] = None
    portfolio_url: Optional[str] = None

    for src, d in normalized_sources:
        if d.get("github_url") and not github_url:
            github_url = d["github_url"]
            record("links.github", src, "direct", 0.95)
        blog = d.get("blog", "")
        if blog:
            blog_type = d.get("blog_type") or classify_url(blog)
            if blog_type == "linkedin" and not linkedin_url:
                linkedin_url = blog
                record("links.linkedin", src, "direct", 0.85)
            elif blog_type == "portfolio" and not portfolio_url:
                portfolio_url = blog
                record("links.portfolio", src, "direct", 0.75)

    links = Links(
        linkedin=linkedin_url,
        github=github_url,
        portfolio=portfolio_url,
    )

    # ── headline ─────────────────────────────────────────────────────────────
    # Build from GitHub bio or CSV title — whichever is richer.
    headline_candidates = [
        ("github", d.get("bio", ""))    for src, d in normalized_sources if src == "github"
    ] + [
        ("csv", d.get("title", ""))     for src, d in normalized_sources if src == "csv"
    ]
    headline, hl_src, hl_conf = _resolve_scalar("bio", headline_candidates)
    if headline:
        record("headline", hl_src, "merged_highest_confidence", hl_conf)

    # ── years_experience ─────────────────────────────────────────────────────
    years_exp: Optional[float] = None
    yoe_src = "none"
    yoe_conf = 0.0
    for src, d in normalized_sources:
        val = d.get("years_experience") or d.get("total_experience")
        if val and float(val) > 0:
            c = _confidence(src, "experience")
            if c > yoe_conf:
                years_exp = float(val)
                yoe_src = src
                yoe_conf = c
    if years_exp is not None:
        record("years_experience", yoe_src, "merged_highest_confidence", yoe_conf)

    # ── skills (union, canonical names) ──────────────────────────────────────
    skill_map: dict[str, Skill] = {}  # canonical_name → Skill

    for src, d in normalized_sources:
        raw_skills: list[str] = d.get("skills", [])
        src_conf = _confidence(src, "skills")
        for raw in raw_skills:
            canonical = canonicalize_skill(raw)
            if not canonical:
                continue
            if canonical in skill_map:
                existing = skill_map[canonical]
                # Boost confidence slightly if multiple sources agree
                new_conf = min(1.0, max(existing.confidence, src_conf) + 0.05)
                skill_map[canonical] = Skill(
                    name=canonical,
                    confidence=new_conf,
                    sources=sorted(set(existing.sources) | {src}),
                )
            else:
                skill_map[canonical] = Skill(
                    name=canonical,
                    confidence=src_conf,
                    sources=[src],
                )

    skills = sorted(skill_map.values(), key=lambda s: (-s.confidence, s.name))
    if skills:
        record("skills", "merged", "merged_union",
               sum(s.confidence for s in skills) / len(skills))

    # ── experience ───────────────────────────────────────────────────────────
    experience: list[Experience] = []
    for src, d in normalized_sources:
        if src == "resume":
            company_names = d.get("company_names") or []
            designations  = d.get("designation")   or []
            exps_raw      = d.get("experience")    or []

            if company_names or designations:
                # pyresparser gives parallel lists; zip them
                for i, company in enumerate(company_names):
                    title = designations[i] if i < len(designations) else None
                    experience.append(Experience(company=company, title=title))
                record("experience", src, "direct", _confidence(src, "experience"))
            elif exps_raw:
                # Fall back to raw experience text snippets
                for snippet in exps_raw[:5]:   # cap to avoid noise
                    if isinstance(snippet, str) and snippet.strip():
                        experience.append(Experience(summary=snippet.strip()))
                if experience:
                    record("experience", src, "direct", _confidence(src, "experience"))
        elif src == "csv":
            company = d.get("current_company", "")
            title   = d.get("title", "")
            if company or title:
                # Insert at front (most recent role from CSV)
                experience.insert(0, Experience(company=company, title=title))
                record("experience", src, "direct", _confidence(src, "company"))

    # ── education ────────────────────────────────────────────────────────────
    education: list[Education] = []
    for src, d in normalized_sources:
        if src == "resume":
            college = d.get("college_name", "")
            degrees = d.get("degree") or []
            if college or degrees:
                edu = Education(
                    institution=college or None,
                    degree=degrees[0] if degrees else None,
                )
                education.append(edu)
                record("education", src, "direct", _confidence(src, "education"))

    # ── candidate_id (deterministic) ────────────────────────────────────────
    candidate_id = _stable_candidate_id(emails, github_url)

    # ── overall_confidence ───────────────────────────────────────────────────
    overall = (
        sum(confidence_scores) / len(confidence_scores)
        if confidence_scores
        else 0.0
    )

    return CandidateProfile(
        candidate_id=candidate_id,
        full_name=full_name or None,
        emails=emails,
        phones=phones,
        location=location,
        links=links,
        headline=headline or None,
        years_experience=years_exp,
        skills=skills,
        experience=experience,
        education=education,
        provenance=provenance,
        overall_confidence=round(overall, 4),
    )


# ---------------------------------------------------------------------------
# Private utilities
# ---------------------------------------------------------------------------

def _iter_str_field(d: dict, keys: list[str]) -> list[str]:
    """Yield non-empty string values for the first matching key in d."""
    for k in keys:
        val = d.get(k)
        if val:
            if isinstance(val, str):
                return [val]
            if isinstance(val, list):
                return [str(v) for v in val if v]
    return []


def _extract_city(location_str: str) -> Optional[str]:
    """
    Best-effort: take the first comma-separated segment as the city.
    "San Francisco, CA" → "San Francisco"
    "Beijing"           → "Beijing"
    ""                  → None
    """
    if not location_str:
        return None
    return location_str.split(",")[0].strip() or None
