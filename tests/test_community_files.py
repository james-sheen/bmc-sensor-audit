"""The community files, and the silent failures each one can have.

These three exist to render: GitHub reads `CITATION.cff` to draw *Cite this
repository*, and `CODE_OF_CONDUCT.md` and `CONTRIBUTING.md` to light rows on
*Insights -> Community Standards*. **Every failure mode here is silent.** An
invalid citation file does not error; the widget simply does not appear, and the
sidebar looks the same as it did before the file was added. A link to a file that
does not exist renders as a link. So the checks are here rather than left to a
one-time look at the rendered page, which is a measurement of the day it was taken.

**The load-bearing one is `test_no_version_is_claimed_while_the_repo_is_unreleased`.**
The citation file deliberately carries no `version` and no `date-released` because
the project has no tag and is not on any index. That is a decision, and a decision
recorded only in prose is one that drifts back — so it is pinned, in both
directions: silent while there is no tag, and red the moment one exists without the
file catching up.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CITATION = ROOT / "CITATION.cff"
CONDUCT = ROOT / "CODE_OF_CONDUCT.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"


class TestTheFilesArePresentAndRenderable:
    @pytest.mark.parametrize("path", [CITATION, CONDUCT, CONTRIBUTING])
    def test_the_file_exists_and_is_not_empty(self, path):
        assert path.is_file(), f"{path.name} is absent"
        assert path.read_text().strip(), f"{path.name} is empty"

    def test_the_citation_file_parses(self):
        """An unparseable `CITATION.cff` does not error anywhere a human sees --
        the sidebar widget just never appears."""
        yaml = pytest.importorskip(
            "yaml", reason="PyYAML parses the citation file; CI installs it")
        data = yaml.safe_load(CITATION.read_text())
        assert isinstance(data, dict), "the citation file is not a mapping"

    def test_the_citation_file_carries_what_the_format_requires(self):
        yaml = pytest.importorskip("yaml", reason="CI installs PyYAML")
        data = yaml.safe_load(CITATION.read_text())
        for key in ("cff-version", "message", "title", "authors"):
            assert data.get(key), f"CITATION.cff has no {key!r}"
        assert data["cff-version"] == "1.2.0"
        assert isinstance(data["authors"], list) and data["authors"]
        for author in data["authors"]:
            assert author.get("family-names") or author.get("name")


class TestTheCitationDoesNotClaimAReleaseThatDoesNotExist:
    """The deliberate omission, pinned so it cannot drift in either direction."""

    @staticmethod
    def _tags() -> list[str] | None:
        """Repository tags, or None when git cannot answer.

        Two failure modes and only one is a return code: a checkout with no
        `.git` exits non-zero, and an image with no git BINARY raises. Answering
        `[]` for either would turn *cannot tell* into *there are no tags*, which
        is the assertion this class is built on.
        """
        try:
            listed = subprocess.run(["git", "tag"], cwd=str(ROOT),
                                    capture_output=True, text=True)
        except OSError:
            return None
        if listed.returncode != 0:
            return None
        return [line for line in listed.stdout.split() if line]

    def test_no_version_is_claimed_while_the_repo_is_unreleased(self):
        yaml = pytest.importorskip("yaml", reason="CI installs PyYAML")
        tags = self._tags()
        if tags is None:
            pytest.skip("git cannot answer here; *cannot tell* is not *no tags*")
        data = yaml.safe_load(CITATION.read_text())
        if tags:
            assert data.get("version"), (
                f"the repository now has tags {tags} and CITATION.cff still "
                f"claims no version; add `version:` and `date-released:` in the "
                f"commit that tags a release")
        else:
            assert "version" not in data, (
                "CITATION.cff claims a version while the repository has no tag; "
                "a citation file should not assert a release that does not exist")
            assert "date-released" not in data, (
                "CITATION.cff claims a release date while the repository has no tag")

    def test_the_readme_still_says_the_project_is_unreleased(self):
        """Non-vacuity. If the README ever announces a release while the tag and
        the citation file both say otherwise, three records of one fact disagree
        and this is where that surfaces."""
        readme = (ROOT / "README.md").read_text()
        assert "not yet released" in readme or "Not yet released" in readme or \
               "no tagged version" in readme, (
            "the README no longer says the project is unreleased; re-check "
            "CITATION.cff's omitted version against it")


class TestNoLinkPointsAtNothing:
    """A markdown link to a missing file renders as a link. It fails when a
    reader clicks it, which is not a moment any check is watching."""

    @pytest.mark.parametrize("path", [CONDUCT, CONTRIBUTING])
    def test_every_repo_relative_link_resolves(self, path):
        targets = re.findall(r"\]\(([^)#:]+)\)", path.read_text())
        missing = [t for t in targets if not (ROOT / t).exists()]
        assert missing == [], f"{path.name} links to {missing}, which do not exist"

    def test_the_conduct_file_routes_reports_at_a_real_channel(self):
        """The whole file is a pointer. If it points at nothing, it is worse than
        absent: it claims a reporting route exists."""
        body = CONDUCT.read_text()
        assert "SECURITY.md" in body
        assert (ROOT / "SECURITY.md").is_file()
        assert "private" in (ROOT / "SECURITY.md").read_text().lower()


class TestContributingMatchesTheRepositoryItDescribes:
    def test_the_hook_command_names_the_directory_that_exists(self):
        """Derived, not transcribed. The suite genuinely fails without the hook,
        so a contributor following a wrong path here meets a red they cannot
        explain."""
        assert "core.hooksPath" in CONTRIBUTING.read_text()
        named = re.search(r"core\.hooksPath\s+(\S+)", CONTRIBUTING.read_text())
        assert named, "the hook command no longer names a path"
        assert (ROOT / named.group(1)).is_dir(), (
            f"CONTRIBUTING points core.hooksPath at {named.group(1)!r}, "
            f"which is not a directory in this repository")

    def test_it_states_no_test_count_of_its_own(self):
        """A second copy of a number that already has one enforced home. The
        README's Tests row is under test; a figure repeated here would drift out
        of sight, and this file is not covered by that assertion."""
        assert not re.search(r"\b\d{3,}\b", CONTRIBUTING.read_text()), (
            "CONTRIBUTING states a 3+ digit figure; the README's Tests row is "
            "the enforced home for counts")
