"""Tests for the commit-message check.

A commit message is the one published surface that can never be corrected, and
until now nothing looked at it. The hygiene check guards files and cannot see the
message, so a token or a site-specific identifier could reach permanent history
through the only door with no lock on it.

Every rule gets a plant and a near-miss, the same discipline as the hygiene rules,
and for the same reason: a check that refuses honest messages gets `--no-verify`d,
and `--no-verify` disables the pre-commit hygiene sweep as well. **A noisy rule
here does not merely get ignored — it trains people to switch off the rules that
are not noisy.**
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import commit_msg_check  # noqa: E402

GOOD = "Refuse a parse that read files declaring nothing\n\nA body that explains why.\n"


def _check(text: str):
    return commit_msg_check.check(text)


class TestObjectiveRefusals:
    """These are refusals because they are decidable, not because they matter most."""

    def test_an_overlong_subject_is_refused(self):
        refusals, _ = _check("x" * 73 + "\n\nbody\n")
        assert any("characters" in r for r in refusals)

    def test_a_subject_of_exactly_the_limit_is_accepted(self):
        """The boundary, so the limit is the documented one and not one less."""
        refusals, _ = _check("x" * 72 + "\n\nbody\n")
        assert refusals == []

    def test_a_subject_ending_in_a_full_stop_is_refused(self):
        refusals, _ = _check("Fix the thing.\n\nbody\n")
        assert any("full stop" in r for r in refusals)

    def test_a_missing_blank_line_after_the_subject_is_refused(self):
        refusals, _ = _check("Fix the thing\nbody on line two\n")
        assert any("blank line" in r for r in refusals)

    def test_a_subject_only_message_is_accepted(self):
        """Not every commit needs a body, and demanding one produces filler."""
        refusals, _ = _check("Fix the thing\n")
        assert refusals == []

    def test_an_empty_message_is_refused(self):
        refusals, _ = _check("\n#  comment only\n")
        assert refusals == ["the message is empty"]


class TestTheLeakRulesReachTheMessage:
    """The point of the whole file: the hygiene vocabulary, applied to a surface it
    could not otherwise see."""

    @pytest.mark.parametrize("body,label", [
        ('HOST = "10.42.7.19"', "RFC1918 address"),  # hygiene: synthetic
        ('MAC = "de:ad:be:ef:00:11"', "MAC address"),  # hygiene: synthetic
        ("notes in /home/someone/notes.txt", "home directory path"),  # hygiene: synthetic
    ])
    def test_each_leak_shape_is_refused(self, body, label):
        refusals, _ = _check(f"Fix the thing\n\n{body}\n")
        assert refusals, f"{label} reached the message unchallenged"

    def test_the_matched_text_is_never_repeated_in_the_refusal(self):
        """Echoing a secret into the terminal publishes it to a scrollback buffer
        and a CI log. Report the length, never the value."""
        secret = "github_pat_11AAAA0aaaaaaaaaaaa_bbbbbbbbbbbbbbbbbbbb"  # hygiene: synthetic
        refusals, _ = _check(f"Fix the thing\n\ntoken {secret}\n")
        assert refusals
        assert all(secret not in r for r in refusals)
        assert any("characters" in r for r in refusals)

    def test_a_git_comment_line_is_not_scanned(self):
        """Git strips `#` lines before storing. Scanning them would refuse on the
        template's own text, which is the checker finding its own scaffolding."""
        refusals, _ = _check(
            "Fix the thing\n\nbody\n# a comment mentioning 10.42.7.19\n")  # hygiene: synthetic
        assert refusals == []


class TestTheDerivedCountNote:
    """A heuristic, so it warns and never refuses. Measured against the real
    history first: it flagged five numbers across five messages, three of them
    genuine and two not, which is nowhere near clean enough to block on."""

    def test_a_bare_derived_count_is_noted_not_refused(self):
        refusals, warnings = _check("Fix the thing\n\nMeasured 247 configurations\n")
        assert refusals == []
        assert any("247" in w for w in warnings)

    def test_a_count_with_a_basis_is_silent(self):
        _, warnings = _check(
            "Fix the thing\n\nMeasured 247 configs (openbmc/entity-manager@0ada0483)\n")
        assert warnings == []

    def test_a_copyright_year_is_not_a_derived_count(self):
        """The false positive that made the first cut of this rule too noisy."""
        _, warnings = _check("Fix the thing\n\nCopyright 2018 Intel Corporation\n")
        assert warnings == []

    def test_a_small_number_is_not_flagged(self):
        """Two of, three files, eight rails — prose numbers a reader can hold."""
        _, warnings = _check("Fix the thing\n\nBoth of the two defects survive.\n")
        assert warnings == []


class TestTheHookIsWiredUp:
    def test_the_hook_exists_and_is_executable(self):
        import os
        hook = ROOT / ".githooks" / "commit-msg"
        assert hook.is_file(), "no commit-msg hook"
        assert os.access(hook, os.X_OK), "git will skip a non-executable hook silently"

    def test_the_hook_calls_the_checker_the_repo_ships(self):
        body = (ROOT / ".githooks" / "commit-msg").read_text()
        assert "tools/commit_msg_check.py" in body
        assert (ROOT / "tools" / "commit_msg_check.py").is_file()

    def test_the_good_message_passes_clean(self):
        """The noise floor. If a well-formed message trips anything, the whole
        hook gets bypassed and takes the leak rules with it."""
        refusals, warnings = _check(GOOD)
        assert refusals == []
        assert warnings == []

    def test_mood_is_documented_as_unenforceable(self):
        """The convention is real and this deliberately does not enforce it. If a
        future change adds a mood heuristic, the docstring saying it cannot be done
        must be corrected in the same change."""
        doc = commit_msg_check.__doc__ or ""
        assert "imperative mood" in doc
        assert "not checkable" in doc or "not enforced" in doc
