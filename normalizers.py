"""
normalizers.py — Stage 2: Concurrent Normalization

Stateless, pure functions. Each takes a raw string and returns a normalized
value (or None for unparseable input — never invented values).

Designed to be called independently via asyncio.gather() across fields,
but also safe to call synchronously in tests.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Skill alias dictionary — maps lowercase raw strings to canonical names.
# Loaded once at module import; can be extended at runtime via load_skill_aliases().
# ---------------------------------------------------------------------------
SKILL_ALIASES: dict[str, str] = {
    # Python ecosystem
    "python":           "Python",
    "py":               "Python",
    "python3":          "Python",
    # JavaScript ecosystem
    "javascript":       "JavaScript",
    "js":               "JavaScript",
    "ecmascript":       "JavaScript",
    "typescript":       "TypeScript",
    "ts":               "TypeScript",
    "nodejs":           "Node.js",
    "node.js":          "Node.js",
    "node":             "Node.js",
    "react":            "React",
    "react.js":         "React",
    "reactjs":          "React",
    "next.js":          "Next.js",
    "nextjs":           "Next.js",
    "vue":              "Vue.js",
    "vue.js":           "Vue.js",
    "angular":          "Angular",
    # Go
    "golang":           "Go",
    "go":               "Go",
    # Data / ML
    "ml":               "Machine Learning",
    "machine learning": "Machine Learning",
    "deep learning":    "Deep Learning",
    "dl":               "Deep Learning",
    "nlp":              "Natural Language Processing",
    "natural language processing": "Natural Language Processing",
    "cv":               "Computer Vision",
    "computer vision":  "Computer Vision",
    "data science":     "Data Science",
    "ds":               "Data Science",
    # Databases
    "postgres":         "PostgreSQL",
    "postgresql":       "PostgreSQL",
    "mysql":            "MySQL",
    "mongo":            "MongoDB",
    "mongodb":          "MongoDB",
    "redis":            "Redis",
    "sqlite":           "SQLite",
    # Cloud / DevOps
    "aws":              "AWS",
    "amazon web services": "AWS",
    "gcp":              "Google Cloud Platform",
    "google cloud":     "Google Cloud Platform",
    "azure":            "Microsoft Azure",
    "k8s":              "Kubernetes",
    "kubernetes":       "Kubernetes",
    "docker":           "Docker",
    "ci/cd":            "CI/CD",
    "devops":           "DevOps",
    # Web / markup
    "html":             "HTML",
    "html5":            "HTML",
    "css":              "CSS",
    "css3":             "CSS",
    "sql":              "SQL",
    # Systems
    "c++":              "C++",
    "cpp":              "C++",
    "c#":               "C#",
    "csharp":           "C#",
    "java":             "Java",
    "rust":             "Rust",
    "scala":            "Scala",
    "kotlin":           "Kotlin",
    "swift":            "Swift",
    # Tools
    "git":              "Git",
    "linux":            "Linux",
}


def load_skill_aliases(csv_path: str) -> dict[str, str]:
    """
    Load additional skill aliases from a CSV file with columns: raw,canonical.
    Merges into the global SKILL_ALIASES dict and returns the merged dict.
    """
    import csv as _csv
    from pathlib import Path

    path = Path(csv_path)
    if not path.exists():
        logger.warning("Skills alias CSV not found: %s", csv_path)
        return SKILL_ALIASES

    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = _csv.DictReader(fh)
            for row in reader:
                raw = (row.get("raw") or "").strip().lower()
                canonical = (row.get("canonical") or "").strip()
                if raw and canonical:
                    SKILL_ALIASES[raw] = canonical
    except Exception as exc:
        logger.warning("Could not load skill aliases from %s: %s", csv_path, exc)

    return SKILL_ALIASES


# ---------------------------------------------------------------------------
# Individual normalizers
# ---------------------------------------------------------------------------

def normalize_phone(raw: str, default_region: str = "US") -> Optional[str]:
    """
    Parse a phone number string and return E.164 format (e.g. "+15555551234").
    Returns None if the number cannot be parsed — never invents a value.

    Handles international formats:
      "(86) 155-9376-9871" → "+8615593769871"   (China number with region hint)
      "1 872 985 1814"     → "+18729851814"      (US number)
      "+44 20 7946 0958"   → "+442079460958"     (UK number, no region needed)
    """
    if not raw:
        return None
    try:
        import phonenumbers

        # Try parsing without region hint first (handles numbers with country code).
        try:
            parsed = phonenumbers.parse(raw, None)
        except phonenumbers.NumberParseException:
            parsed = phonenumbers.parse(raw, default_region)

        if not phonenumbers.is_valid_number(parsed):
            logger.debug("Phone '%s' parsed but is not a valid number.", raw)
            return None

        return phonenumbers.format_number(
            parsed, phonenumbers.PhoneNumberFormat.E164
        )
    except ImportError:
        logger.warning(
            "phonenumbers library not installed; phone '%s' left as-is.", raw
        )
        return raw or None
    except Exception as exc:
        logger.debug("Could not parse phone '%s': %s", raw, exc)
        return None


def normalize_email(raw: str) -> Optional[str]:
    """
    Lowercase, strip, and validate an email address.
    Returns None for syntactically invalid addresses.
    Does NOT do DNS / MX validation — that would be a network call.
    """
    if not raw:
        return None
    normalized = raw.strip().lower()
    # Minimal structural check: one '@', at least one '.' after '@'
    at_idx = normalized.find("@")
    if at_idx <= 0:
        return None
    domain_part = normalized[at_idx + 1:]
    if "." not in domain_part or domain_part.startswith("."):
        return None
    return normalized


def normalize_name(raw: str) -> str:
    """
    Titlecase and collapse internal whitespace.
    "  HAIZHI  GENG  " → "Haizhi Geng"
    """
    if not raw:
        return ""
    return " ".join(raw.strip().title().split())


def normalize_country(raw: str) -> Optional[str]:
    """
    Return an ISO-3166-1 alpha-2 country code (e.g. "US", "CN").
    Accepts: "US", "usa", "United States", "China", "CN", etc.
    Returns None for unrecognizable input.
    """
    if not raw:
        return None
    # Fast path: already a 2-letter code.
    stripped = raw.strip()
    if len(stripped) == 2 and stripped.isalpha():
        return stripped.upper()

    try:
        import pycountry
        results = pycountry.countries.search_fuzzy(stripped)
        if results:
            return results[0].alpha_2
    except ImportError:
        logger.warning("pycountry not installed; country '%s' returned as-is.", raw)
        return stripped.upper()[:2] if len(stripped) >= 2 else None
    except LookupError:
        logger.debug("Could not resolve country from '%s'.", raw)

    return None


def canonicalize_skill(raw: str, aliases: Optional[dict[str, str]] = None) -> str:
    """
    Map a raw skill string to its canonical name using the alias dict.
    Falls back to title-casing the raw string if no alias is found.
    """
    if not raw:
        return ""
    table = aliases if aliases is not None else SKILL_ALIASES
    key = raw.strip().lower()
    return table.get(key, raw.strip().title())


def normalize_date(raw: str) -> Optional[str]:
    """
    Parse a loose date string and return YYYY-MM format.
    Accepts: "Jan 2021", "2021-01", "01/2021", "January 2021", "2021", etc.
    Returns None if unparseable.
    """
    if not raw:
        return None
    raw = raw.strip()
    if raw.lower() in ("present", "current", "now"):
        return "present"

    try:
        from dateutil import parser as _dateparser
        dt = _dateparser.parse(raw, default=None)  # type: ignore[arg-type]
        if dt:
            return dt.strftime("%Y-%m")
    except ImportError:
        logger.warning("python-dateutil not installed; date '%s' returned as-is.", raw)
        return raw
    except Exception:
        pass

    # Fallback: bare 4-digit year → YYYY-01
    if re.fullmatch(r"\d{4}", raw):
        return f"{raw}-01"

    logger.debug("Could not parse date from '%s'.", raw)
    return None


def classify_url(url: str) -> str:
    """
    Classify a URL into one of: "linkedin" | "github" | "portfolio" | "other".
    Used to route the GitHub 'blog' field into the correct Links sub-field.
    """
    if not url:
        return "other"
    lower = url.lower()
    if "linkedin.com" in lower:
        return "linkedin"
    if "github.com" in lower:
        return "github"
    return "portfolio"


# ---------------------------------------------------------------------------
# Per-source normalization dispatcher
# ---------------------------------------------------------------------------

def normalize_raw_candidate_sync(source: str, data: dict) -> dict:
    """
    Apply the appropriate normalizers to each field of a raw extracted dict.
    Returns a new dict with normalized values — never mutates the input.

    This is the synchronous version; engine.py wraps it in asyncio.to_thread()
    to run normalization for all candidate slots concurrently.
    """
    out: dict = dict(data)  # shallow copy

    if source == "csv":
        out["name"]  = normalize_name(data.get("name", ""))
        out["email"] = normalize_email(data.get("email", "")) or ""
        out["phone"] = normalize_phone(data.get("phone", ""))
        # Leave current_company and title as-is after stripping
        out["current_company"] = (data.get("current_company") or "").strip()
        out["title"]           = (data.get("title") or "").strip()

    elif source == "github":
        out["name"]     = normalize_name(data.get("name", ""))
        out["email"]    = normalize_email(data.get("email", "")) or ""
        out["location"] = (data.get("location") or "").strip()
        out["country"]  = normalize_country(data.get("location", ""))
        out["skills"]   = [
            canonicalize_skill(s)
            for s in data.get("skills_from_languages", [])
        ]
        # Classify the blog URL
        blog = data.get("blog", "")
        out["blog_type"] = classify_url(blog)

    elif source == "resume":
        out["name"]  = normalize_name(data.get("name", ""))
        out["email"] = normalize_email(data.get("email", "")) or ""
        out["phone"] = normalize_phone(data.get("mobile_number", ""))
        out["skills"] = [
            canonicalize_skill(s)
            for s in data.get("skills", [])
            if s  # guard against None entries pyresparser sometimes returns
        ]
        out["years_experience"] = float(data.get("total_experience") or 0.0)

    return out


async def normalize_raw_candidate(source: str, data: dict) -> dict:
    """
    Async wrapper: runs normalization in a thread pool so multiple candidates
    can be normalized concurrently in Stage 2.
    """
    return await asyncio.to_thread(normalize_raw_candidate_sync, source, data)
