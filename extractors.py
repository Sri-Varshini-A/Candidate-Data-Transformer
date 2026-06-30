"""
extractors.py — Stage 1: Concurrent Extraction

Implements a Hybrid Scatter-Gather pattern:
  - Outer level: all N candidates are processed simultaneously.
  - Inner level: for each candidate, CSV / GitHub / resume run simultaneously.

All synchronous libraries (PyGithub, pyresparser) are wrapped in
asyncio.to_thread() so they don't block the event loop.

Contract: no exception ever propagates to the caller. Every failure
is recorded in RawCandidate.errors and the pipeline continues.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import re
from pathlib import Path
from typing import Optional

from models import RawCandidate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Confidence weights — defined here because the extractor knows what each
# source is good at. Imported by merger.py for conflict resolution.
# ---------------------------------------------------------------------------
SOURCE_CONFIDENCE: dict[str, dict[str, float]] = {
    "csv": {
        "email":   0.9,
        "phone":   0.8,
        "name":    0.7,
        "company": 0.9,
        "title":   0.9,
    },
    "github": {
        "name":     0.6,
        "skills":   0.9,
        "bio":      0.8,
        "location": 0.6,
    },
    "resume": {
        "skills":     0.85,
        "education":  0.85,
        "experience": 0.80,
        "name":       0.70,
        "email":      0.80,
        "phone":      0.75,
    },
}

# Deterministic tie-breaker when confidence weights are equal.
SOURCE_PRIORITY: list[str] = ["csv", "resume", "github"]


# ---------------------------------------------------------------------------
# Internal utility
# ---------------------------------------------------------------------------

def _wrap_result(source: str, result: object, timeout_s: float) -> RawCandidate:
    """Convert a gather result (value or exception) into a RawCandidate."""
    if isinstance(result, asyncio.TimeoutError):
        return RawCandidate(
            source=source,
            data={},
            errors=[f"Source '{source}' timed out after {timeout_s:.1f}s."],
        )
    if isinstance(result, BaseException):
        return RawCandidate(
            source=source,
            data={},
            errors=[
                f"Source '{source}' raised an unexpected error: "
                f"{type(result).__name__}: {result}"
            ],
        )
    # result is already a properly returned RawCandidate
    return result  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 1. CSV EXTRACTOR  (stdlib csv)
# ---------------------------------------------------------------------------

def _extract_csv_rows_sync(csv_path: str) -> tuple[list[RawCandidate], list[str]]:
    """
    Synchronous inner function — directly testable without an event loop.

    Returns:
      (candidates, file_errors)
      candidates   — one RawCandidate per data row; empty on file-level failure.
      file_errors  — non-empty only when the file itself cannot be read.
    """
    file_errors: list[str] = []
    path = Path(csv_path)

    if not path.exists():
        file_errors.append(f"CSV file not found: {csv_path}")
        return [], file_errors

    try:
        # utf-8-sig strips the invisible BOM that Excel adds to UTF-8 exports.
        # Without this, the first column header gets a "﻿" prefix.
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
    except UnicodeDecodeError as exc:
        file_errors.append(
            f"CSV encoding error — try saving the file as UTF-8: {exc}"
        )
        return [], file_errors
    except csv.Error as exc:
        file_errors.append(f"CSV parse error: {exc}")
        return [], file_errors
    except OSError as exc:
        file_errors.append(f"CSV I/O error: {exc}")
        return [], file_errors

    if not rows:
        file_errors.append(
            "CSV file has no data rows (possibly only a header or completely empty)."
        )
        return [], file_errors

    expected_cols = {"name", "email", "phone", "current_company", "title"}
    candidates: list[RawCandidate] = []

    for i, row in enumerate(rows):
        row_errors: list[str] = []

        # Normalize keys: strip surrounding whitespace, lowercase.
        data = {
            k.strip().lower(): (v.strip() if v else "")
            for k, v in row.items()
            if k  # guard against None keys from malformed CSVs
        }

        missing = expected_cols - set(data.keys())
        if missing:
            row_errors.append(
                f"Row {i + 1}: missing expected columns {sorted(missing)}. "
                "Those fields will be empty for this candidate."
            )

        candidates.append(
            RawCandidate(source="csv", data=data, errors=row_errors)
        )

    return candidates, file_errors


async def extract_csv_rows(csv_path: str) -> list[RawCandidate]:
    """
    Async wrapper around _extract_csv_rows_sync.

    Returns one RawCandidate per CSV data row.
    On a file-level failure, returns a single RawCandidate with empty data
    so the caller always receives a list and can index into it safely.
    """
    rows, file_errors = await asyncio.to_thread(_extract_csv_rows_sync, csv_path)
    if file_errors:
        # Represent the whole-file failure as a single slot with errors
        return [RawCandidate(source="csv", data={}, errors=file_errors)]
    return rows


# ---------------------------------------------------------------------------
# 2. GITHUB EXTRACTOR  (PyGithub — synchronous network I/O, must use to_thread)
# ---------------------------------------------------------------------------

def _parse_github_username(url_or_username: str) -> Optional[str]:
    """
    Accept any of:
      "https://github.com/username"
      "github.com/username"
      "username"

    Returns the bare username string, or None if the input is unparseable.
    """
    s = url_or_username.strip().rstrip("/")
    if not s:
        return None

    # If there's no slash it's already a bare username.
    if "/" not in s:
        return s

    # Strip scheme prefix if present.
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break

    # Strip "github.com/" host.
    if s.startswith("github.com/"):
        s = s[len("github.com/"):]

    # The username is the first path segment (ignore any sub-paths like /repos).
    username = s.split("/")[0].strip()
    return username if username else None


def _extract_github_sync(
    github_url: str,
    token: Optional[str] = None,
) -> RawCandidate:
    """
    Synchronous inner function — wraps PyGithub's blocking API.

    Handles:
      - UnknownObjectException (HTTP 404) — user not found or profile is private.
      - GithubException 403             — rate limit exceeded or bad token.
      - GithubException 401             — token invalid / expired.
      - All other exceptions            — caught and stored in errors.
    """
    errors: list[str] = []
    data: dict = {}

    try:
        from github import Github
        from github.GithubException import GithubException, UnknownObjectException
    except ImportError:
        errors.append(
            "PyGithub is not installed. Run: pip install PyGithub"
        )
        return RawCandidate(source="github", data=data, errors=errors)

    username = _parse_github_username(github_url)
    if not username:
        errors.append(
            f"Could not parse a GitHub username from the input: {github_url!r}. "
            "Expected a URL like 'https://github.com/username' or a bare username."
        )
        return RawCandidate(source="github", data=data, errors=errors)

    try:
        g = Github(token) if token else Github()
        # Unauthenticated requests are rate-limited to 60/hr. Authenticated: 5000/hr.
        user = g.get_user(username)

        # Eagerly read .name to trigger the first real API request.
        # UnknownObjectException surfaces here if the user doesn't exist.
        _ = user.name

        repos = list(user.get_repos())

        # Tally programming languages across all public repos.
        language_counts: dict[str, int] = {}
        for repo in repos:
            lang = repo.language
            if lang:
                language_counts[lang] = language_counts.get(lang, 0) + 1

        blog = (user.blog or "").strip()

        data = {
            "name":                  user.name or "",
            "bio":                   user.bio or "",
            "location":              user.location or "",
            "email":                 user.email or "",
            "blog":                  blog,
            "company":               (user.company or "").strip(),
            "public_repos":          user.public_repos,
            "repos":                 [r.name for r in repos],
            "languages":             language_counts,        # {"Python": 5, "Go": 2, …}
            "skills_from_languages": list(language_counts.keys()),
            "github_url":            f"https://github.com/{username}",
            "avatar_url":            user.avatar_url or "",
        }

    except UnknownObjectException:
        errors.append(
            f"GitHub user '{username}' not found (HTTP 404). "
            "Verify the username/URL and that the profile is public."
        )
    except GithubException as exc:
        if exc.status == 403:
            errors.append(
                f"GitHub API rate limit exceeded or token is invalid (HTTP 403). "
                f"Pass --token <PAT> to raise the limit to 5 000 req/hr. "
                f"Detail: {exc.data}"
            )
        elif exc.status == 401:
            errors.append(
                f"GitHub token is invalid or has expired (HTTP 401). "
                f"Generate a new token at https://github.com/settings/tokens. "
                f"Detail: {exc.data}"
            )
        else:
            errors.append(
                f"GitHub API error (HTTP {exc.status}): {exc.data}"
            )
    except Exception as exc:  # noqa: BLE001
        errors.append(
            f"Unexpected error while fetching GitHub profile for '{username}': "
            f"{type(exc).__name__}: {exc}"
        )

    return RawCandidate(source="github", data=data, errors=errors)


async def extract_github(
    github_url: str,
    token: Optional[str] = None,
) -> RawCandidate:
    """
    Async wrapper: PyGithub blocks the event loop — run it in a thread pool.
    asyncio.to_thread() uses the default ThreadPoolExecutor (Python 3.9+).
    """
    return await asyncio.to_thread(_extract_github_sync, github_url, token)


# ---------------------------------------------------------------------------
# 3. RESUME EXTRACTOR  (pyresparser — synchronous NLP, must use to_thread)
# ---------------------------------------------------------------------------

def _extract_resume_sync(
    resume_path: str,
    skills_file: Optional[str] = None,
) -> RawCandidate:
    """
    Synchronous inner function — wraps pyresparser's CPU-heavy NLP pipeline.

    pyresparser.get_extracted_dict() returns a dict with these keys:
      name, email, mobile_number, skills, college_name, degree,
      designation, experience, company_names, total_experience

    All values may be None — we coerce them to "" or [] so downstream code
    never needs to None-guard individual fields.
    """
    errors: list[str] = []
    data: dict = {}

    try:
        from pyresparser import ResumeParser
    except ImportError:
        errors.append(
            "pyresparser is not installed. "
            "Run: pip install pyresparser\n"
            "Then: python -m spacy download en_core_web_sm"
        )
        return RawCandidate(source="resume", data=data, errors=errors)

    path = Path(resume_path)

    if not path.exists():
        errors.append(f"Resume file not found: {resume_path}")
        return RawCandidate(source="resume", data=data, errors=errors)

    if path.suffix.lower() not in (".pdf", ".docx"):
        errors.append(
            f"Unsupported resume format '{path.suffix}'. "
            "pyresparser only supports .pdf and .docx files."
        )
        return RawCandidate(source="resume", data=data, errors=errors)

    kwargs: dict = {}
    if skills_file:
        skills_path = Path(skills_file)
        if skills_path.exists():
            kwargs["skills_file"] = str(skills_path)
        else:
            errors.append(
                f"Skills vocabulary CSV not found at '{skills_file}'. "
                "Falling back to pyresparser's built-in skills list."
            )

    try:
        parser = ResumeParser(str(path), **kwargs)
        raw: Optional[dict] = parser.get_extracted_dict()

        if not raw:
            errors.append(
                "pyresparser returned no data. "
                "The file may be empty, image-only, or password-protected."
            )
            return RawCandidate(source="resume", data={}, errors=errors)

        # Coerce all None/missing values so the merger never sees None.
        data = {
            "name":             raw.get("name") or "",
            "email":            raw.get("email") or "",
            "mobile_number":    raw.get("mobile_number") or "",
            "skills":           raw.get("skills") or [],
            "college_name":     raw.get("college_name") or "",
            "degree":           raw.get("degree") or [],
            "designation":      raw.get("designation") or [],
            "experience":       raw.get("experience") or [],
            "company_names":    raw.get("company_names") or [],
            "total_experience": float(raw.get("total_experience") or 0.0),
        }

    except Exception as exc:  # noqa: BLE001
        # Common failure modes:
        #   OSError        — spaCy model not downloaded
        #   PDFSyntaxError — corrupted or password-protected PDF
        #   PackagesNotFoundError — missing NLP dependencies
        errors.append(
            f"Resume parsing failed ({type(exc).__name__}): {exc}. "
            "If the spaCy model is missing, run: python -m spacy download en_core_web_sm"
        )
        # data stays {} — a partial extraction is not reliable

    return RawCandidate(source="resume", data=data, errors=errors)


async def extract_resume(
    resume_path: str,
    skills_file: Optional[str] = None,
) -> RawCandidate:
    """
    Async wrapper: pyresparser is CPU-heavy — run in a thread pool so the
    event loop can drive other concurrent extractions in parallel.
    """
    return await asyncio.to_thread(_extract_resume_sync, resume_path, skills_file)


# ---------------------------------------------------------------------------
# 4. ENTITY RESOLUTION + BATCH COORDINATION
# ---------------------------------------------------------------------------

def _read_github_file(github_file: str) -> list[str]:
    """
    Read a plain-text file that contains one GitHub URL per line.
    Blank lines and lines starting with '#' are ignored.
    """
    path = Path(github_file)
    if not path.exists():
        logger.warning("GitHub profile file not found: %s", github_file)
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]


def _norm_name_for_matching(name: str) -> str:
    """Lowercase + collapse whitespace for case-insensitive name comparison."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _match_score(github_raw: RawCandidate, csv_raw: RawCandidate) -> int:
    """
    Score how confidently a GitHub profile belongs to a CSV candidate.

    Returns
    ───────
    3 — exact email match (very high confidence)
    2 — normalized full-name exact match (high confidence)
    1 — GitHub username is a prefix of the CSV email local-part, ≥ 6 chars
        e.g. "senthilpalanisamy" → matches "senthilpalanisamy2020@u.nw.edu"
        Minimum length guard prevents short usernames causing false positives.
    0 — no reliable signal found
    """
    g = github_raw.data
    c = csv_raw.data

    github_email = (g.get("email") or "").lower().strip()
    csv_email    = (c.get("email") or "").lower().strip()

    # Score 3: exact email match
    if github_email and csv_email and github_email == csv_email:
        return 3

    # Score 2: normalized full-name match
    github_name = _norm_name_for_matching(g.get("name") or "")
    csv_name    = _norm_name_for_matching(c.get("name") or "")
    if github_name and csv_name and github_name == csv_name:
        return 2

    # Score 1: username is a substantial prefix of the CSV email local-part.
    # We check startswith (not contains) to avoid the "potato" false-positive:
    # "ghzpotato" does NOT start with "jmpotato" → no spurious match.
    username = _parse_github_username(g.get("github_url") or "")
    if username and len(username) >= 6 and "@" in csv_email:
        local_part = csv_email.split("@")[0].lower()
        if local_part.startswith(username.lower()):
            return 1

    return 0


def resolve_entities(
    csv_rows:        list[RawCandidate],
    github_profiles: list[RawCandidate],
    resumes:         list[RawCandidate],
) -> list[list[RawCandidate]]:
    """
    Match GitHub profiles to their correct CSV row using explicit identity
    signals — never relying on input file order.

    Matching phases (in priority order)
    ─────────────────────────────────────
    Phase 1 — High confidence: exact email OR normalized full-name match.
    Phase 2 — Medium confidence: GitHub username is a prefix (≥ 6 chars) of
               the CSV email local-part.
    Phase 3 — Elimination: if exactly ONE CSV row and ONE GitHub profile both
               remain unmatched, pair them by exclusion and emit a warning.
    Phase 4 — Any still-unmatched GitHub profiles are dropped with a warning;
               they are NOT silently injected into a wrong candidate slot.

    Resumes are matched by position. The user passes --resume files in an
    explicit, intentional order, so positional mapping is unambiguous.
    """
    n = len(csv_rows)
    github_assignment: list[Optional[int]] = [None] * n  # csv_index → github_profiles index
    github_used: set[int] = set()

    def _best_available(csv_idx: int, min_score: int) -> tuple[int, int]:
        best_j, best_score = -1, 0
        for j, gh in enumerate(github_profiles):
            if j in github_used:
                continue
            sc = _match_score(gh, csv_rows[csv_idx])
            if sc >= min_score and sc > best_score:
                best_j, best_score = j, sc
        return best_j, best_score

    # Phase 1 — email / name match (score ≥ 2)
    for i in range(n):
        j, sc = _best_available(i, min_score=2)
        if j >= 0:
            github_assignment[i] = j
            github_used.add(j)
            logger.debug(
                "Entity resolution Phase 1: github_profiles[%d]='%s' → "
                "csv_rows[%d]='%s' (score=%d)",
                j, github_profiles[j].data.get("github_url"),
                i, csv_rows[i].data.get("name"), sc,
            )

    # Phase 2 — username-prefix match (score == 1) for still-unmatched rows
    for i in range(n):
        if github_assignment[i] is not None:
            continue
        j, sc = _best_available(i, min_score=1)
        if j >= 0:
            github_assignment[i] = j
            github_used.add(j)
            logger.debug(
                "Entity resolution Phase 2: github_profiles[%d]='%s' → "
                "csv_rows[%d]='%s' (score=%d)",
                j, github_profiles[j].data.get("github_url"),
                i, csv_rows[i].data.get("name"), sc,
            )

    # Phase 3 — elimination: exactly 1 unmatched GitHub ↔ 1 unmatched CSV row
    unmatched_csv    = [i for i in range(n) if github_assignment[i] is None]
    unmatched_github = [j for j in range(len(github_profiles)) if j not in github_used]

    if len(unmatched_csv) == 1 and len(unmatched_github) == 1:
        i, j = unmatched_csv[0], unmatched_github[0]
        github_assignment[i] = j
        github_used.add(j)
        logger.warning(
            "Entity resolution Phase 3 (elimination): '%s' assigned to CSV "
            "candidate '%s' — no direct identity signal was found. Verify "
            "this pairing is correct.",
            github_profiles[j].data.get("github_url", "?"),
            csv_rows[i].data.get("name", "?"),
        )

    # Phase 4 — any remaining unmatched GitHub profiles are dropped
    for j in range(len(github_profiles)):
        if j not in github_used:
            logger.warning(
                "GitHub profile '%s' could not be matched to any CSV "
                "candidate and will be excluded from the output.",
                github_profiles[j].data.get("github_url", "?"),
            )

    # Assemble per-candidate source lists
    result: list[list[RawCandidate]] = []
    for i, csv_raw in enumerate(csv_rows):
        sources: list[RawCandidate] = [csv_raw]
        if github_assignment[i] is not None:
            sources.append(github_profiles[github_assignment[i]])
        # Resumes: positional — the user provided them in explicit order
        if i < len(resumes):
            sources.append(resumes[i])
        result.append(sources)

    return result


async def extract_all_candidates(
    csv_path:        Optional[str]       = None,
    github_file:     Optional[str]       = None,
    resume_paths:    Optional[list[str]] = None,
    github_token:    Optional[str]       = None,
    skills_file:     Optional[str]       = None,
    timeout_seconds: float               = 30.0,
) -> list[list[RawCandidate]]:
    """
    Top-level batch extractor with entity-resolution-based matching.

    Architecture change from naive positional pairing
    ──────────────────────────────────────────────────
    GitHub profiles are fetched INDEPENDENTLY of CSV row order, then matched
    to the correct candidate via explicit identity signals (email → name →
    username prefix → elimination by exclusion). This prevents the input
    file ordering of githubprofile.txt from causing cross-pollination.

    Concurrency model
    ─────────────────
    All GitHub profile fetches and all resume parses run concurrently in one
    asyncio.gather() call (the synchronization barrier). CSV rows are read
    first (cheap synchronous I/O) so we have a stable candidate count before
    kicking off the expensive concurrent work.

    Returns list[list[RawCandidate]] — one inner list per candidate, containing
    only the sources that were successfully matched to that candidate.
    """
    resume_paths = resume_paths or []

    # ── Step 1: Read CSV rows (cheap, synchronous) ────────────────────────
    csv_rows: list[RawCandidate] = []
    if csv_path:
        csv_rows = await extract_csv_rows(csv_path)
        for rc in csv_rows:
            if rc.errors:
                logger.warning("CSV source warnings: %s", rc.errors)

    # ── Step 2: Parse GitHub URL list ────────────────────────────────────
    github_urls: list[str] = []
    if github_file:
        github_urls = _read_github_file(github_file)

    if not csv_rows and not github_urls and not resume_paths:
        logger.warning("No sources provided to extract_all_candidates().")
        return []

    # ── Step 3: Fetch all GitHub profiles + all resumes concurrently ──────
    # They are collected as independent lists; entity resolution pairs them.
    concurrent_coros: list = []
    n_github = len(github_urls)   # first n_github results → GitHub profiles

    for url in github_urls:
        concurrent_coros.append(
            asyncio.wait_for(extract_github(url, token=github_token), timeout_seconds)
        )
    for path in resume_paths:
        concurrent_coros.append(
            asyncio.wait_for(extract_resume(path, skills_file=skills_file), timeout_seconds)
        )

    if concurrent_coros:
        # Synchronization barrier: waits for ALL concurrent fetches.
        # return_exceptions=True prevents one failed fetch from cancelling siblings.
        raw_results = await asyncio.gather(*concurrent_coros, return_exceptions=True)
    else:
        raw_results = []

    github_profiles: list[RawCandidate] = [
        _wrap_result("github", raw_results[i], timeout_seconds)
        for i in range(n_github)
    ]
    resumes: list[RawCandidate] = [
        _wrap_result("resume", raw_results[i], timeout_seconds)
        for i in range(n_github, len(raw_results))
    ]

    # ── Step 4: Entity resolution — match GitHub profiles to CSV rows ─────
    if csv_rows:
        matched = resolve_entities(csv_rows, github_profiles, resumes)
    else:
        # No CSV anchor: pair by position as a best-effort fallback
        n = max(len(github_profiles), len(resumes))
        matched = []
        for i in range(n):
            sources: list[RawCandidate] = []
            if i < len(github_profiles):
                sources.append(github_profiles[i])
            if i < len(resumes):
                sources.append(resumes[i])
            matched.append(sources)

    logger.info(
        "Stage 1 complete: %d candidate(s), %d total source(s) "
        "[csv=%s, github_file=%s, resumes=%d].",
        len(matched),
        sum(len(m) for m in matched),
        csv_path or "—",
        github_file or "—",
        len(resume_paths),
    )
    return matched
