"""The community files, and the silent failures each one can have.

These three exist to render: GitHub reads `CITATION.cff` to draw *Cite this
repository*, and `CODE_OF_CONDUCT.md` and `CONTRIBUTING.md` to light rows on
*Insights -> Community Standards*. **Every failure mode here is silent.** An
invalid citation file does not error; the widget simply does not appear, and the
sidebar looks the same as it did before the file was added. A link to a file that
does not exist renders as a link. So the checks are here rather than left to a
one-time look at the rendered page, which is a measurement of the day it was taken.

**The load-bearing class is `TestTheReleaseRecordsAgree`.** The version is written
down four times — the package literal, `CITATION.cff`, the README, and the tag —
because four different readers each look in a different one, and a number written
more than once drifts. That class holds them to each other in both directions, so
neither a citation file claiming a release that does not exist nor a README still
calling a shipped package unreleased can survive a test run.
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


class TestTheReleaseRecordsAgree:
    """One fact, four records, pinned against each other in both directions.

    **The anchor is the version literal, not `git tag`.** It used to be the tag,
    and that could not survive its own release: every one of these assertions was
    green only in two consistent states -- untagged with a README saying so, or
    tagged with the citation file caught up -- and the commit that moves between
    them necessarily has no tag yet, because the tag is made *of* it. The gate
    was therefore red exactly once per release, at the moment it was most likely
    to be waved through with `--no-verify`.

    A tree-local anchor has no such ordering problem, and it keeps answering in
    the two places a tag is not available: an sdist, and a shallow CI checkout
    where `git tag` returns nothing and looks identical to a repository that
    genuinely has none. `fetch-depth: 0` in `checks.yml` is what makes the tag
    cross-check below meaningful rather than vacuous on CI.
    """

    UNRELEASED = "0.0.0"

    @staticmethod
    def _version() -> str:
        from bmc_sensor_audit import __version__
        return __version__

    @staticmethod
    def _tags() -> list[str] | None:
        """Repository tags, or None when git cannot answer.

        Two failure modes and only one is a return code: a checkout with no
        `.git` exits non-zero, and an image with no git BINARY raises. Answering
        `[]` for either would turn *cannot tell* into *there are no tags*.

        A third state answers successfully and is still not an answer: a shallow
        clone fetched without tags reports none. That is why nothing below reads
        an empty list as evidence of anything.
        """
        try:
            listed = subprocess.run(["git", "tag"], cwd=str(ROOT),
                                    capture_output=True, text=True)
        except OSError:
            return None
        if listed.returncode != 0:
            return None
        return [line for line in listed.stdout.split() if line]

    def test_the_version_has_exactly_one_home(self):
        """`pyproject.toml` reads the package literal instead of repeating it.
        Restoring a static version here is not a style regression -- it recreates
        two numbers that agreed by luck until the first bump touched only one."""
        pyproject = (ROOT / "pyproject.toml").read_text()
        assert re.search(r"^dynamic\s*=\s*\[[^]]*[\"']version[\"']", pyproject,
                         re.MULTILINE), \
            "pyproject.toml no longer declares the version dynamic"
        assert not re.search(r"^version\s*=", pyproject, re.MULTILINE), (
            "pyproject.toml declares a static version as well as a dynamic one; "
            "the literal in src/bmc_sensor_audit/__init__.py is the only home")

    def test_the_citation_file_matches_the_released_state(self):
        yaml = pytest.importorskip("yaml", reason="CI installs PyYAML")
        data = yaml.safe_load(CITATION.read_text())
        version = self._version()
        if version == self.UNRELEASED:
            assert "version" not in data, (
                "CITATION.cff claims a version while the package still reports "
                f"{self.UNRELEASED}; a citation file should not assert a release "
                "that does not exist")
            assert "date-released" not in data, (
                "CITATION.cff claims a release date while the package still "
                f"reports {self.UNRELEASED}")
        else:
            assert data.get("version") == version, (
                f"the package reports {version} and CITATION.cff says "
                f"{data.get('version')!r}; both are published records of one fact")
            assert data.get("date-released"), (
                "CITATION.cff carries a version and no `date-released:`")
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(data["date-released"])), (
                f"date-released is {data['date-released']!r}; CFF 1.2.0 types it "
                f"as a YYYY-MM-DD string, and an unquoted date deserialises to a "
                f"date object that fails the schema while parsing fine here")

    def test_the_readme_matches_the_released_state(self):
        """Non-vacuity. The README is the one record a reader meets first, and it
        is also the long description on the index page -- so it is wrong in the
        most public place available if it disagrees with the artifact."""
        readme = (ROOT / "README.md").read_text()
        version = self._version()
        unreleased_wording = ("not yet released" in readme
                              or "Not yet released" in readme
                              or "no tagged version" in readme)
        if version == self.UNRELEASED:
            assert unreleased_wording, (
                "the README no longer says the project is unreleased while the "
                f"package still reports {self.UNRELEASED}")
        else:
            assert not unreleased_wording, (
                f"the package reports {version} and the README still describes "
                f"an unreleased project")
            assert f"Released — {version}" in readme, (
                f"the README does not announce {version}; Status should lead with "
                f"`**Released — {version}**` so this has one place to look")

    #: The TOOL's tag namespace. `action-vX.Y.Z` versions the GitHub Action apart
    #: from the package and must never be read here: one namespace serving two
    #: artifacts is how a repository ends up unable to release its own 1.0, and
    #: comparing the action's versions against the package's would recreate the
    #: conflation that split exists to prevent.
    TOOL_TAG = re.compile(r"^v(\d+(?:\.\d+)*)$")

    @classmethod
    def _tool_versions(cls, tags: list[str]) -> list[tuple[int, ...]]:
        return [tuple(int(part) for part in match.group(1).split("."))
                for match in (cls.TOOL_TAG.match(tag) for tag in tags) if match]

    @staticmethod
    def _named_tag() -> str | None:
        found = re.search(r"tagged `([^`]+)`", (ROOT / "README.md").read_text())
        return found.group(1) if found else None

    def test_the_readme_names_the_tag_this_version_will_carry(self):
        """The dropped-`v` guard, and it is TREE-LOCAL on purpose.

        **This half used to be welded to the existence check below, and inherited
        its ordering problem for no reason.** Whether the README's tag string
        agrees with the version literal is answerable from the tree alone: in an
        sdist, in a shallow checkout with no tags, and at every instant of a
        release. It was previously reachable only when `git tag` returned
        something, so the environments where a record is most likely to drift
        unnoticed were the ones not checking it.

        A dropped leading `v` is how these two part company, and the failure is
        silent: `0.1.1` and `v0.1.1` look alike enough that a reader confirms the
        version and never notices the tag they were given does not exist.
        """
        version = self._version()
        named = self._named_tag()
        if version == self.UNRELEASED:
            assert named is None, (
                f"the README names the tag {named!r} while the package still "
                f"reports {self.UNRELEASED}; an unreleased tree must not hand a "
                f"reader a tag to check out")
            return
        assert named, (
            f"the package reports {version} and the README names no tag. Status "
            f"should read: tagged `v{version}`")
        assert named == f"v{version}", (
            f"the README names the tag {named!r} and the package reports "
            f"{version}; they must be `v{version}`. A leading v dropped from one "
            f"of the two is the usual way this happens")

    def test_a_tag_and_the_tree_do_not_disagree(self):
        """The existence half, which is the ONLY part that cannot be answered from
        the tree -- and it is now allowed to say so.

        **What was wrong with this before.** Its own docstring said the release
        commit is legitimately untagged for as long as it takes to tag it, and its
        code tolerated only a repository with NO TAGS AT ALL -- which stopped being
        true at 0.1.0. So it went red between the commit and the tag, every
        release, at exactly the moment somebody is most likely to reach for
        `--no-verify`. That is the failure the version-literal re-anchor removed
        from the rest of this class; this one assertion never got it.

        Worse than red, it also RACED. CI checks out with `fetch-depth: 0` and
        fetches whatever tags the remote has at that instant; the runbook pushes
        master and then the tag, so the release commit's own CI run passes or fails
        on which of the two won. 0.1.1's run passed that way, on timing.

        **The window is carved out precisely rather than widened.** Only this
        version may be untagged, and only while no later version is tagged -- a
        release in flight is always the newest one. Reverting a bump while leaving
        its tag, or tagging from the wrong commit, both leave a later tag behind
        and still fail here.

        **Whether the tag was ever MADE is a fact about the remote, not about this
        tree**, and no assertion here can reach it. The release runbook checks it
        after pushing, and `/releases/latest` shows it. Saying that out loud in the
        skip is the honest version; asserting it from a working tree would be a
        check that is right by luck.
        """
        tags = self._tags()
        if not tags:
            pytest.skip("no tags visible here; *cannot tell* is not *no tags*")
        version = self._version()
        assert version != self.UNRELEASED, (
            f"the repository has tags {tags} and the package still reports "
            f"{self.UNRELEASED}")

        if f"v{version}" in tags:
            return
        current = tuple(int(part) for part in version.split("."))
        ahead = sorted(t for t in self._tool_versions(tags) if t > current)
        assert not ahead, (
            f"v{version} has no tag, and {['v' + '.'.join(map(str, t)) for t in ahead]} "
            f"name later versions. A release in flight is the only reason this "
            f"version should be untagged, and a release in flight is always the "
            f"newest one -- so either a bump was reverted with its tag left behind, "
            f"or a tag was made from the wrong commit")
        pytest.skip(
            f"v{version} is not tagged in this tree. The tag is made OF the commit "
            f"that sets the version literal, so this is the one legitimate window "
            f"and `git tag -a v{version}` closes it. Whether the tag was ever made "
            f"is a fact about the remote rather than this tree: the release runbook "
            f"checks it after pushing.")


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
