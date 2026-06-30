# EightFold.ai Candidate Data Transformer

Ingests candidate data from a Recruiter CSV, GitHub profile URLs, and PDF/DOCX
resumes, then emits one canonical JSON profile per candidate with full
provenance tracking.

---

## Setup

**Python 3.11+ required.**

```bash
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

Set your GitHub Personal Access Token in the environment to avoid the
unauthenticated rate limit of 60 requests/hour:

```bash
export GITHUB_TOKEN=ghp_...        # macOS / Linux
$env:GITHUB_TOKEN = "ghp_..."      # Windows PowerShell
```

---

## How to Run

### Minimal — CSV only

```bash
python cli.py --csv "sample_data/Recruiter CSV export.csv" --pretty
```

### Full — CSV + GitHub profiles + resumes

```bash
python cli.py \
  --csv        "sample_data/Recruiter CSV export.csv" \
  --github-file sample_data/githubprofile.txt \
  --resume     "sample_data/claude-resume1.pdf" \
              "sample_data/claude-resume2.pdf" \
  --token      "$GITHUB_TOKEN" \
  --pretty
```

### Write to file instead of stdout

```bash
python cli.py --csv "sample_data/Recruiter CSV export.csv" \
              --output results.json --pretty
```

### Use a custom projection schema

```bash
python cli.py --csv "sample_data/Recruiter CSV export.csv" \
              --config configs/custom.json --pretty
```

`configs/custom.json` produces a flat ATS-ready payload: first email only,
first phone only, no nested confidence or provenance fields.

### Full options reference

```
--csv PATH             Recruiter CSV (one row per candidate)
--github-file PATH     Plain-text file of GitHub profile URLs, one per line
--resume PATH [...]    PDF or DOCX resume files (positionally matched to CSV rows)
--skills-csv PATH      Optional raw→canonical skill alias CSV
--token GITHUB_PAT     GitHub PAT (default: $GITHUB_TOKEN)
--config PATH          Projection config JSON (default: configs/default.json)
--timeout SECONDS      Per-source extraction timeout (default: 30)
--output PATH          Write JSON to file instead of stdout
--pretty               Pretty-print with 2-space indent
--log-level LEVEL      DEBUG | INFO | WARNING | ERROR (default: WARNING)
```

---

## Output on the Sample Inputs

Command run:

```bash
python cli.py --csv "sample_data/Recruiter CSV export.csv" --pretty
```

Sample input (`sample_data/Recruiter CSV export.csv`):

```
name,email,phone,current_company,title
HAIZHI GENG   ,ghzpotato@gmail.com,(86) 155-9376-9871,PingCAP Inc.,Scheduling R&D Intern
SENTHIL PALANISAMY,senthilpalanisamy2020@u.northwestern.edu,1 872 985 1814,Geomagical labs- Augumented Reality,Applied Research Engineer
```

Actual output (two candidates, CSV-only run):

```json
[
  {
    "candidate_id": "cand_cbe161183be8fc1c",
    "full_name": "Haizhi Geng",
    "emails": ["ghzpotato@gmail.com"],
    "phones": ["(86) 155-9376-9871"],
    "location": { "city": null, "region": null, "country": null },
    "links": { "linkedin": null, "github": null, "portfolio": null, "other": [] },
    "headline": "Scheduling R&D Intern",
    "years_experience": null,
    "skills": null,
    "experience": [
      { "company": "PingCAP Inc.", "title": "Scheduling R&D Intern",
        "start": null, "end": null, "summary": null }
    ],
    "education": null,
    "overall_confidence": 0.72,
    "provenance": [
      { "field": "full_name",  "source": "csv", "method": "merged_highest_confidence" },
      { "field": "emails",     "source": "csv", "method": "merged_union" },
      { "field": "phones",     "source": "csv", "method": "merged_union" },
      { "field": "headline",   "source": "csv", "method": "merged_highest_confidence" },
      { "field": "experience", "source": "csv", "method": "direct" }
    ]
  },
  {
    "candidate_id": "cand_2a2ecff675f7d6ff",
    "full_name": "Senthil Palanisamy",
    "emails": ["senthilpalanisamy2020@u.northwestern.edu"],
    "phones": ["1 872 985 1814"],
    "location": { "city": null, "region": null, "country": null },
    "links": { "linkedin": null, "github": null, "portfolio": null, "other": [] },
    "headline": "Applied Research Engineer",
    "years_experience": null,
    "skills": null,
    "experience": [
      { "company": "Geomagical labs- Augumented Reality", "title": "Applied Research Engineer",
        "start": null, "end": null, "summary": null }
    ],
    "education": null,
    "overall_confidence": 0.72,
    "provenance": [
      { "field": "full_name",  "source": "csv", "method": "merged_highest_confidence" },
      { "field": "emails",     "source": "csv", "method": "merged_union" },
      { "field": "phones",     "source": "csv", "method": "merged_union" },
      { "field": "headline",   "source": "csv", "method": "merged_highest_confidence" },
      { "field": "experience", "source": "csv", "method": "direct" }
    ]
  }
]
```

**Known nulls in this output.** The `phonenumbers` library was not installed in
the run environment, so phone numbers are emitted verbatim rather than in E.164
format. The `experience.start`, `experience.end`, `experience.summary`,
`education`, and `skills` fields are null by design in this sprint: the
normalization layer passes pyresparser's raw parallel lists directly to the
merger without an intermediate text-parsing handler, so date strings and degree
sub-components are never extracted. When GitHub profiles and resumes are added
as sources, `links.github`, `skills`, `years_experience`, and `education` are
populated; `experience.start` and `experience.end` remain null.

---

## Tests

```bash
python -m pytest tests/ -v
```

Expected result — all 16 tests pass in under 1 second:

```
tests/test_entity_resolution.py::TestMatchScore::test_email_match_returns_3            PASSED
tests/test_entity_resolution.py::TestMatchScore::test_name_match_returns_2             PASSED
tests/test_entity_resolution.py::TestMatchScore::test_username_prefix_returns_1        PASSED
tests/test_entity_resolution.py::TestMatchScore::test_no_match_returns_0               PASSED
tests/test_entity_resolution.py::TestMatchScore::test_potato_false_positive_prevented  PASSED
tests/test_entity_resolution.py::TestMatchScore::test_short_username_excluded          PASSED
tests/test_entity_resolution.py::TestResolveEntities::test_reversed_github_file_order_corrected     PASSED
tests/test_entity_resolution.py::TestResolveEntities::test_correct_github_file_order_also_works     PASSED
tests/test_entity_resolution.py::TestResolveEntities::test_elimination_fallback_when_no_direct_signal PASSED
tests/test_entity_resolution.py::TestResolveEntities::test_unmatched_github_profile_not_injected     PASSED
tests/test_entity_resolution.py::TestResolveEntities::test_resumes_matched_positionally              PASSED
tests/test_entity_resolution.py::TestResolveEntities::test_no_github_profiles_returns_csv_only_slots PASSED
tests/test_entity_resolution.py::TestNormName::test_all_caps_normalized                PASSED
tests/test_entity_resolution.py::TestNormName::test_extra_whitespace_collapsed         PASSED
tests/test_entity_resolution.py::TestNormName::test_empty_string                       PASSED
tests/test_entity_resolution.py::TestNormName::test_none_safe                          PASSED

16 passed in 0.15s
```

### What the tests cover

**TestMatchScore (6 tests)** — unit tests for the `_match_score` scoring
function that drives entity resolution. Verifies that an exact email match
returns score 3, a normalized name match returns score 2, and a GitHub
username-prefix match against the CSV email local-part returns score 1.
Includes two regression guards: one preventing the "potato" substring
false-positive (where `JmPotato` and `ghzpotato@gmail.com` share a substring
but must not match), and one enforcing the six-character minimum length guard
on the prefix heuristic.

**TestResolveEntities (6 tests)** — integration tests for the `resolve_entities`
function covering the full four-phase matching algorithm. The primary regression
test reproduces the exact cross-pollination bug: `githubprofile.txt` has
Senthil's URL on line 1 and Haizhi's on line 2, while the CSV has Haizhi on
row 1 and Senthil on row 2; entity resolution must restore the correct pairing
regardless of file order. Additional tests cover the Phase 3 elimination
fallback (when no direct signal exists for one remaining pair), the rule that
unmatched GitHub profiles are dropped with a warning rather than silently
injected into a wrong candidate slot, positional resume matching, and
CSV-only operation with no GitHub input.

**TestNormName (4 tests)** — unit tests for `_norm_name_for_matching`, which
lowercases and collapses whitespace before name comparisons. Covers all-caps
input, leading/trailing/internal whitespace, empty string, and None input.
