"""
cli.py — Command-Line Interface

Thin entry point: parses arguments and delegates to engine.run_pipeline_sync().
All heavy logic lives in the engine and its stages.

Usage examples
──────────────
# Default schema — both sample candidates
python cli.py \\
  --csv "sample_data/Recruiter CSV export.csv" \\
  --github-file "sample_data/githubprofile.txt" \\
  --resume "sample_data/claude-resume1.pdf" "sample_data/claude-resume2.pdf" \\
  --config configs/default.json \\
  --token $GITHUB_TOKEN

# ATS-ready custom schema
python cli.py \\
  --csv "sample_data/Recruiter CSV export.csv" \\
  --github-file "sample_data/githubprofile.txt" \\
  --resume "sample_data/claude-resume1.pdf" "sample_data/claude-resume2.pdf" \\
  --config configs/custom.json

# CSV only (no GitHub, no resume)
python cli.py --csv "sample_data/Recruiter CSV export.csv"
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from engine import run_pipeline_sync


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="transformer",
        description=(
            "Multi-Source Candidate Data Transformer\n"
            "Ingests CSV, GitHub profiles, and PDF/DOCX resumes into one "
            "canonical JSON profile per candidate."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── Sources ────────────────────────────────────────────────────────────
    src = p.add_argument_group("Input sources")
    src.add_argument(
        "--csv",
        metavar="PATH",
        help="Recruiter CSV file. Multiple rows = multiple candidates.",
    )
    src.add_argument(
        "--github-file",
        metavar="PATH",
        help=(
            "Plain-text file containing GitHub profile URLs, one per line "
            "(e.g. sample_data/githubprofile.txt). "
            "Positionally matched to CSV rows."
        ),
    )
    src.add_argument(
        "--resume",
        metavar="PATH",
        nargs="+",
        help=(
            "One or more PDF/DOCX resume files. "
            "Positionally matched to CSV rows."
        ),
    )
    src.add_argument(
        "--skills-csv",
        metavar="PATH",
        help=(
            "Optional CSV with columns 'raw,canonical' to canonicalize "
            "skill names during resume extraction."
        ),
    )

    # ── Auth ───────────────────────────────────────────────────────────────
    auth = p.add_argument_group("Authentication")
    auth.add_argument(
        "--token",
        metavar="GITHUB_PAT",
        default=os.environ.get("GITHUB_TOKEN"),
        help=(
            "GitHub Personal Access Token. Raises rate limit from 60 to "
            "5 000 requests/hr. Defaults to $GITHUB_TOKEN env var."
        ),
    )

    # ── Config ─────────────────────────────────────────────────────────────
    cfg = p.add_argument_group("Pipeline configuration")
    cfg.add_argument(
        "--config",
        metavar="PATH",
        default="configs/default.json",
        help="Projection config JSON (default: configs/default.json).",
    )
    cfg.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="Per-source extraction timeout in seconds (default: 30).",
    )

    # ── Output ─────────────────────────────────────────────────────────────
    out = p.add_argument_group("Output")
    out.add_argument(
        "--output",
        metavar="PATH",
        help="Write JSON to this file instead of stdout.",
    )
    out.add_argument(
        "--pretty",
        action="store_true",
        default=True,
        help="Pretty-print JSON with 2-space indent (default: on).",
    )
    out.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="WARNING",
        help="Logging verbosity (default: WARNING).",
    )

    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s  %(name)s: %(message)s",
    )

    # Require at least one source
    if not any([args.csv, args.github_file, args.resume]):
        parser.error(
            "Provide at least one source: --csv, --github-file, or --resume."
        )

    results = run_pipeline_sync(
        csv_path               = args.csv,
        github_file            = args.github_file,
        resume_paths           = args.resume or [],
        github_token           = args.token,
        skills_file            = args.skills_csv,
        projection_config_path = args.config,
        timeout_seconds        = args.timeout,
    )

    indent = 2 if args.pretty else None
    output_json = json.dumps(results, indent=indent, default=str, ensure_ascii=False)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_json, encoding="utf-8")
        print(f"Output written to {out_path}", file=sys.stderr)
    else:
        print(output_json)


if __name__ == "__main__":
    main()
