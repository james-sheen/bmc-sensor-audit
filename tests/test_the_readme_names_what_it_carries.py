"""Two README claims about this distribution, held against the files that make
them true. Both were reported from outside, and neither could have gone red.

**The Licence section named one of the two things this depends on.** It said
*the same terms as `arbiter-engine`* and stopped there. `presence-audit` became
a REQUIRED dependency when the neutral half was split out of this package, and
the paragraph a consumer reads to work out what they are taking on never
mentioned it. The section is a claim about a set, and the set is declared three
files away -- so the check below derives the set instead of transcribing it,
which means adding a dependency licenses naming it in the same edit.

**The tag table carried a version.** The row read ``v0.2.5 | the tool, on PyPI``
while this package shipped 0.3.1 -- two releases stale, contradicting the pin in
the row directly beneath it, and stale for the second time. Nothing could have
caught it: every version cross-check in `test_community_files.py` keys on
``tagged `...` `` in the Status line, and this cell is not that. It is not
guarded here either. It is REMOVED: the table exists to say which of two tag
namespaces is which, the prose above it already spells the shape, and a version
in that cell is a transcription with no oracle anywhere. What is asserted is
that it stays a shape.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
ACTION = (ROOT / "action.yml").read_text(encoding="utf-8")

sys.path.insert(0, str(ROOT / "tools"))
import runtime_deps                                     # noqa: E402


def _name_of(spec: str) -> str:
    """The distribution name out of a requirement string."""
    return re.split(r"[<>=!~;\s\[]", spec, 1)[0].strip().lower()


def _optional_groups() -> dict:
    """The `[project.optional-dependencies]` table, group -> specs."""
    found = re.search(r"^\[project\.optional-dependencies\]$", PYPROJECT, re.M)
    if not found:
        return {}
    table = PYPROJECT[found.end():].split("\n[", 1)[0]
    return {group: re.findall(r'"([^"]+)"', body)
            for group, body in re.findall(r"^(\w+)\s*=\s*\[([^\]]*)\]", table, re.M)}


def _extras_the_action_installs() -> set:
    """Which extras a consumer of the shipped action ends up with.

    This is what makes the set below DERIVED rather than a list with `dev`
    struck off by hand. A required dependency reaches every consumer; an extra
    reaches the ones the action installs, and `action.yml` says which those
    are. `dev` is absent from that answer because nothing ships it, not because
    somebody typed an exception.
    """
    out = set()
    for spec in re.findall(r"spec='([^']+)'", ACTION):
        out.update(re.findall(r"\[([^\]]+)\]", spec))
    return {extra.strip() for group in out for extra in group.split(",")}


def _distributions_a_consumer_acquires() -> set:
    """Every distribution installing this one can put in somebody's tree."""
    names = {_name_of(spec) for spec in
             runtime_deps.runtime_dependencies(PYPROJECT)}
    groups = _optional_groups()
    for extra in _extras_the_action_installs():
        names.update(_name_of(spec) for spec in groups.get(extra, []))
    return names


def _licence_section() -> str | None:
    found = re.search(r"^## Licence\n(.*?)(?=^## |\Z)", README, re.S | re.M)
    return found.group(1) if found else None


def _tag_table_rows() -> list:
    """Rows of the table that distinguishes the two tag namespaces."""
    return [line for line in README.splitlines()
            if line.startswith("|") and "`" in line and "|---" not in line]


class TestTheLicenceSectionNamesEveryDependency:
    """A licence paragraph is a claim about a SET, and the set is declared
    elsewhere."""

    def test_there_is_a_section_and_a_set_to_check_it_against(self):
        """NON-VACUITY, twice over: a renamed heading and an unparsed
        dependency table each turn the check below green over nothing, and
        the second is the likelier one -- the derivation reads three files."""
        assert _licence_section() is not None, "the README has no Licence section"
        acquired = _distributions_a_consumer_acquires()
        assert acquired, (
            "no dependency was derived from pyproject.toml and action.yml, so "
            "the check below would quantify over an empty set")
        assert len(acquired) >= 2, (
            f"only {sorted(acquired)} derived; this package has a required "
            f"dependency and an extra the action installs, and the defect "
            f"being guarded was a section that named exactly one of them")

    def test_the_section_names_each_of_them(self):
        section = _licence_section()
        missing = sorted(name for name in _distributions_a_consumer_acquires()
                         if f"`{name}`" not in section)
        assert missing == [], (
            f"the Licence section does not name {missing}. Installing this "
            f"package puts them in a consumer's tree, and the section is where "
            f"they look to find out what they have taken on")

    def test_that_check_can_produce_a_positive(self):
        """Before believing a negative, prove the probe can produce one. This
        runs the same predicate over the paragraph as it was written, which
        named the engine and not the core."""
        was = ("The same terms as `arbiter-engine`, which this depends on from "
               "Stage 2, and as OpenBMC's own `entity-manager`, which it reads.")
        missing = sorted(name for name in _distributions_a_consumer_acquires()
                         if f"`{name}`" not in was)
        assert missing, (
            "the predicate reads the paragraph this check exists to have "
            "caught as complete, so its silence on the current one means "
            "nothing")


class TestTheTagTableStatesNamespacesAndNotInstances:
    """The table answers *which namespace is this tag in*. A version in it
    answers a different question, and answers it staler every release."""

    def test_there_are_rows_to_read(self):
        assert len(_tag_table_rows()) >= 2, (
            f"only {len(_tag_table_rows())} table row(s) found; the check "
            f"below would read almost nothing")

    def test_the_tool_row_names_a_shape_and_not_a_release(self):
        """`action-v0` is exempt and is not an exception to this rule: it is a
        MOVING tag, so the string is the tag's actual name rather than a
        transcription of a release. What is refused is a fixed tag naming a
        version this repository has to remember to bump."""
        offenders = []
        for row in _tag_table_rows():
            for tag in re.findall(r"`(v[^`]*)`", row):
                if re.fullmatch(r"v\d+(\.\d+)+", tag) and "moving" not in row:
                    offenders.append(tag)
        assert offenders == [], (
            f"the tag table names {offenders}, a fixed tag pinned to a release. "
            f"Nothing derives that cell, so it goes stale at the next release "
            f"and reads correct until somebody checks it. Say the shape")

    def test_that_check_can_produce_a_positive(self):
        """The literal that was in the cell, run through the same predicate."""
        row = "| `v0.2.5` | the tool, on PyPI | — |"
        found = [tag for tag in re.findall(r"`(v[^`]*)`", row)
                 if re.fullmatch(r"v\d+(\.\d+)+", tag) and "moving" not in row]
        assert found, (
            "the predicate does not see the stale row this check exists to "
            "have caught")
