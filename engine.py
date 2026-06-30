"""
engine.py — Pipeline Orchestrator

Wires the four stages together into a single async function and a
synchronous batch entry point used by cli.py.

Stage 1 — Concurrent Extraction  (extractors.extract_all_candidates)
Stage 2 — Concurrent Normalization (normalizers.normalize_raw_candidate per source)
Stage 3 — Sequential Merging      (merger.merge)
Stage 4 — Sequential Projection   (projector.project)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from extractors import extract_all_candidates
from merger import merge
from models import CandidateProfile, RawCandidate
from normalizers import normalize_raw_candidate
from projector import ProjectionConfig, load_projection_config, project

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Single-candidate pipeline (Stages 2–4)
# ---------------------------------------------------------------------------

async def _process_candidate(
    raw_sources:  list[RawCandidate],
    config:       ProjectionConfig,
) -> dict:
    """
    Stages 2–4 for one candidate.

    Stage 2: Normalize all sources concurrently.
    Stage 3: Merge into one CandidateProfile (sequential, deterministic).
    Stage 4: Project into the output dict (sequential).
    """
    # ── Stage 2: Concurrent normalization ────────────────────────────────────
    norm_tasks = [
        normalize_raw_candidate(rc.source, rc.data)
        for rc in raw_sources
        if rc.data  # skip sources that returned empty data (extraction failed)
    ]
    valid_sources = [rc for rc in raw_sources if rc.data]

    if norm_tasks:
        normalized_dicts = await asyncio.gather(*norm_tasks)
    else:
        normalized_dicts = []

    # Pair source name with its normalized dict; preserve SOURCE_PRIORITY order.
    from extractors import SOURCE_PRIORITY
    normalized_pairs = sorted(
        zip([rc.source for rc in valid_sources], normalized_dicts),
        key=lambda p: (
            SOURCE_PRIORITY.index(p[0]) if p[0] in SOURCE_PRIORITY else len(SOURCE_PRIORITY)
        ),
    )

    # Also log any extraction errors for transparency.
    for rc in raw_sources:
        if rc.errors:
            logger.info(
                "Source '%s' extraction notes: %s",
                rc.source,
                "; ".join(rc.errors),
            )

    # ── Stage 3: Sequential merge ─────────────────────────────────────────────
    profile: CandidateProfile = merge(list(normalized_pairs))

    # ── Stage 4: Sequential projection ───────────────────────────────────────
    output: dict = project(profile, config)

    return output


# ---------------------------------------------------------------------------
# Batch pipeline entry point
# ---------------------------------------------------------------------------

async def run_pipeline(
    csv_path:                Optional[str]       = None,
    github_file:             Optional[str]       = None,
    resume_paths:            Optional[list[str]] = None,
    github_token:            Optional[str]       = None,
    skills_file:             Optional[str]       = None,
    projection_config_path:  Optional[str]       = None,
    timeout_seconds:         float               = 30.0,
) -> list[dict]:
    """
    Full 4-stage pipeline for all candidates.

    Returns a list of projected dicts — one per candidate.
    Each dict conforms to the ProjectionConfig schema.
    Never raises; extraction failures are captured in provenance.
    """
    # Load the projection config once (shared across all candidates).
    config = load_projection_config(projection_config_path)

    # ── Stage 1: Concurrent extraction of all candidates ─────────────────────
    logger.info("Stage 1 — Extracting from all sources …")
    all_raw: list[list[RawCandidate]] = await extract_all_candidates(
        csv_path        = csv_path,
        github_file     = github_file,
        resume_paths    = resume_paths or [],
        github_token    = github_token,
        skills_file     = skills_file,
        timeout_seconds = timeout_seconds,
    )

    if not all_raw:
        logger.warning("No candidates extracted. Check your input paths.")
        return []

    logger.info("Stage 1 complete — %d candidate slot(s) extracted.", len(all_raw))

    # ── Stages 2–4 for each candidate (run candidates concurrently) ───────────
    logger.info("Stages 2–4 — Normalizing, merging, and projecting …")
    candidate_tasks = [
        _process_candidate(raw_sources, config)
        for raw_sources in all_raw
    ]
    results: list[dict] = await asyncio.gather(*candidate_tasks)

    logger.info("Pipeline complete — %d candidate profile(s) produced.", len(results))
    return results


def run_pipeline_sync(
    csv_path:               Optional[str]       = None,
    github_file:            Optional[str]       = None,
    resume_paths:           Optional[list[str]] = None,
    github_token:           Optional[str]       = None,
    skills_file:            Optional[str]       = None,
    projection_config_path: Optional[str]       = None,
    timeout_seconds:        float               = 30.0,
) -> list[dict]:
    """
    Synchronous entry point for cli.py and tests.
    Wraps run_pipeline() in asyncio.run().
    """
    return asyncio.run(
        run_pipeline(
            csv_path               = csv_path,
            github_file            = github_file,
            resume_paths           = resume_paths,
            github_token           = github_token,
            skills_file            = skills_file,
            projection_config_path = projection_config_path,
            timeout_seconds        = timeout_seconds,
        )
    )
