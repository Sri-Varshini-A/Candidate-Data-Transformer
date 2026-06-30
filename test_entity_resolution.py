"""
tests/test_entity_resolution.py

Regression tests for the entity-resolution layer that replaced naive positional
pairing.  These tests directly reproduce the cross-pollination bug where
githubprofile.txt had Senthil's URL on line 1 and Haizhi's on line 2,
but the CSV had Haizhi on row 1 and Senthil on row 2.
"""
from __future__ import annotations

import pytest
from models import RawCandidate
from extractors import _match_score, resolve_entities, _norm_name_for_matching


# ─── shared fixtures ──────────────────────────────────────────────────────────

def _csv(name: str, email: str) -> RawCandidate:
    return RawCandidate(source="csv", data={"name": name, "email": email})


def _github(name: str, email: str, url: str) -> RawCandidate:
    return RawCandidate(source="github", data={"name": name, "email": email, "github_url": url})


HAIZHI_CSV    = _csv("haizhi geng", "ghzpotato@gmail.com")
SENTHIL_CSV   = _csv("senthil palanisamy", "senthilpalanisamy2020@u.northwestern.edu")

HAIZHI_GH     = _github("Haizhi Geng",    "ghzpotato@gmail.com",           "https://github.com/JmPotato")
SENTHIL_GH    = _github("Senthil Palanisamy", "senthillihtnes1994@gmail.com", "https://github.com/senthilpalanisamy")


# ─── _match_score unit tests ──────────────────────────────────────────────────

class TestMatchScore:
    def test_email_match_returns_3(self):
        """Exact email match is the highest-confidence signal."""
        assert _match_score(HAIZHI_GH, HAIZHI_CSV) == 3

    def test_name_match_returns_2(self):
        """Normalized full-name match returns score 2."""
        gh = _github("Senthil Palanisamy", "", "https://github.com/senthilpalanisamy")
        assert _match_score(gh, SENTHIL_CSV) == 2

    def test_username_prefix_returns_1(self):
        """Username that is a prefix of the email local-part returns score 1."""
        # "senthilpalanisamy" is a prefix of "senthilpalanisamy2020@..."
        gh = _github("", "", "https://github.com/senthilpalanisamy")
        assert _match_score(gh, SENTHIL_CSV) == 1

    def test_no_match_returns_0(self):
        """Unrelated GitHub profile returns score 0."""
        # "JmPotato" (8 chars, but "ghzpotato" does NOT start with "jmpotato")
        gh = _github("", "", "https://github.com/JmPotato")
        assert _match_score(gh, SENTHIL_CSV) == 0

    def test_potato_false_positive_prevented(self):
        """
        'ghzpotato' and 'JmPotato' both contain 'potato', but startswith-check
        prevents the spurious match the user reported.
        """
        gh_jmpotato = _github("", "", "https://github.com/JmPotato")
        # "ghzpotato" does NOT start with "jmpotato"
        assert _match_score(gh_jmpotato, HAIZHI_CSV) == 0

    def test_short_username_excluded(self):
        """Usernames shorter than 6 characters are not used for prefix matching."""
        gh = _github("", "", "https://github.com/abc")  # 3-char username
        csv = _csv("abcdef person", "abcdef@example.com")
        assert _match_score(gh, csv) == 0


# ─── resolve_entities — main regression test ─────────────────────────────────

class TestResolveEntities:
    def test_reversed_github_file_order_corrected(self):
        """
        The exact bug: githubprofile.txt has Senthil first, Haizhi second.
        CSV has Haizhi first, Senthil second.
        After entity resolution the correct pairings must be restored.
        """
        # Reversed order — exactly as it appears in sample_data/githubprofile.txt
        github_profiles_reversed = [SENTHIL_GH, HAIZHI_GH]
        csv_rows = [HAIZHI_CSV, SENTHIL_CSV]

        result = resolve_entities(csv_rows, github_profiles_reversed, resumes=[])

        assert len(result) == 2

        haizhi_sources = result[0]
        senthil_sources = result[1]

        haizhi_gh = next(s for s in haizhi_sources if s.source == "github")
        senthil_gh = next(s for s in senthil_sources if s.source == "github")

        assert haizhi_gh.data["github_url"] == "https://github.com/JmPotato", (
            "Haizhi must be linked to JmPotato, not senthilpalanisamy"
        )
        assert senthil_gh.data["github_url"] == "https://github.com/senthilpalanisamy", (
            "Senthil must be linked to senthilpalanisamy, not JmPotato"
        )

    def test_correct_github_file_order_also_works(self):
        """Correct positional order must also produce the right pairing."""
        github_profiles_correct = [HAIZHI_GH, SENTHIL_GH]
        csv_rows = [HAIZHI_CSV, SENTHIL_CSV]

        result = resolve_entities(csv_rows, github_profiles_correct, resumes=[])

        haizhi_gh = next(s for s in result[0] if s.source == "github")
        senthil_gh = next(s for s in result[1] if s.source == "github")

        assert haizhi_gh.data["github_url"] == "https://github.com/JmPotato"
        assert senthil_gh.data["github_url"] == "https://github.com/senthilpalanisamy"

    def test_elimination_fallback_when_no_direct_signal(self):
        """
        If JmPotato has no public email and the display name doesn't match the CSV,
        Phase 3 (elimination) should still correctly pair it with Haizhi once
        Senthil has already been matched by name.
        """
        # JmPotato with no email and a non-matching display name
        haizhi_gh_no_signal = _github("Ji Zheng", "", "https://github.com/JmPotato")

        github_profiles = [SENTHIL_GH, haizhi_gh_no_signal]  # reversed
        csv_rows = [HAIZHI_CSV, SENTHIL_CSV]

        result = resolve_entities(csv_rows, github_profiles, resumes=[])

        haizhi_gh = next(s for s in result[0] if s.source == "github")
        senthil_gh = next(s for s in result[1] if s.source == "github")

        assert haizhi_gh.data["github_url"] == "https://github.com/JmPotato", (
            "Elimination phase must assign JmPotato to Haizhi when it's the only "
            "remaining unmatched pair"
        )
        assert senthil_gh.data["github_url"] == "https://github.com/senthilpalanisamy"

    def test_unmatched_github_profile_not_injected(self):
        """
        If a GitHub profile cannot be matched to any candidate, it must be
        silently dropped — not injected into a wrong candidate slot.
        """
        unknown_gh = _github("Unknown Person", "stranger@example.com", "https://github.com/nobody")
        # Only one CSV row (Haizhi), two GitHub profiles
        result = resolve_entities(
            csv_rows=[HAIZHI_CSV],
            github_profiles=[HAIZHI_GH, unknown_gh],
            resumes=[],
        )

        assert len(result) == 1
        github_sources = [s for s in result[0] if s.source == "github"]
        assert len(github_sources) == 1
        assert github_sources[0].data["github_url"] == "https://github.com/JmPotato"

    def test_resumes_matched_positionally(self):
        """Resumes are matched by the user-provided position, not by entity signals."""
        resume_haizhi = RawCandidate(source="resume", data={"name": "Haizhi Geng"})
        resume_senthil = RawCandidate(source="resume", data={"name": "Senthil Palanisamy"})

        result = resolve_entities(
            csv_rows=[HAIZHI_CSV, SENTHIL_CSV],
            github_profiles=[],
            resumes=[resume_haizhi, resume_senthil],
        )

        haizhi_resume = next(s for s in result[0] if s.source == "resume")
        senthil_resume = next(s for s in result[1] if s.source == "resume")

        assert haizhi_resume.data["name"] == "Haizhi Geng"
        assert senthil_resume.data["name"] == "Senthil Palanisamy"

    def test_no_github_profiles_returns_csv_only_slots(self):
        """No GitHub input → candidates still returned with only CSV source."""
        result = resolve_entities(
            csv_rows=[HAIZHI_CSV, SENTHIL_CSV],
            github_profiles=[],
            resumes=[],
        )

        assert len(result) == 2
        for sources in result:
            assert all(s.source == "csv" for s in sources)


# ─── _norm_name_for_matching ──────────────────────────────────────────────────

class TestNormName:
    def test_all_caps_normalized(self):
        assert _norm_name_for_matching("HAIZHI GENG") == "haizhi geng"

    def test_extra_whitespace_collapsed(self):
        assert _norm_name_for_matching("  Senthil  Palanisamy  ") == "senthil palanisamy"

    def test_empty_string(self):
        assert _norm_name_for_matching("") == ""

    def test_none_safe(self):
        assert _norm_name_for_matching(None) == ""  # type: ignore[arg-type]
