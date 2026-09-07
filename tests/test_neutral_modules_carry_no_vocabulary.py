"""The machinery that is meant to be domain-free no longer names the domain.

Asserted by PARSE. A word-level scan reads a comment mentioning a module as an
import of it, and this repository has already paid for that mistake once.

The population is named explicitly rather than derived from a directory: the
capture half of `inventory/` is BMC by design and always will be, so a rule
saying *no module imports the vocabulary* would be false and a rule saying *no
module in this directory* would be wrong about which directory.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src/bmc_sensor_audit"

#: The modules whose job is the same in any domain. Adding one here is a claim
#: that it has stopped deciding by BMC words, and this file is where that claim
#: gets checked.
NEUTRAL = (
    "core/protocols.py",
    "core/vocabulary.py",
    "core/plugins.py",
    "report.py",
    "detect/generator.py",
    "inventory/regression.py",
)

#: CORRECTED AGAIN 2026-09-07, and the correction went both ways.
#:
#: `report.py` MOVED ONTO this list: extracting the field-strictness capability
#: left it reaching no BMC-only member at all.
#:
#: `inventory/diff.py` and `detect/generator.py` moved OFF it, and
#: `detect/generator.py` moved BACK ON the same day once its peer grouping went
#: to the vertical. It was the mirror test below that said so: the module had
#: stopped reaching BMC-only members and was still labelled mixed.
#:
#: Both errors came from one instrument. The earlier measurement word-searched
#: function source, so it counted `undeclared` inside the string
#: "present, undeclared" and inside the key `undeclared_present`, and it missed
#: attribute access it had no reason to look for. The check below now walks the
#: AST for ATTRIBUTE ACCESS, which cannot read prose as code.
MIXED = ("inventory/diff.py",)

#: Members the other bridge has no counterpart for. A neutral module reaching one
#: is reaching into this domain whether or not it imports its vocabulary.
BMC_ONLY = {"units", "resource", "undeclared", "divergence", "shapes_seen",
            "fields_observed", "is_enabled", "part", "channel"}

VOCABULARY = {"sensor_types", "redfish_schema"}


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                names.update(node.module.split("."))
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.update(a.name.split("."))
    return names


@pytest.mark.parametrize("relative", NEUTRAL)
def test_a_neutral_module_does_not_import_the_vocabulary(relative):
    path = SRC / relative
    assert path.is_file(), f"{relative} moved; this list is now measuring nothing"
    leaked = _imports(path) & VOCABULARY
    assert not leaked, (
        f"{relative} imports {sorted(leaked)}. The vocabulary is supplied by a "
        f"vertical through the registry; importing it here is the coupling this "
        f"module was freed from")


def test_the_list_is_not_empty_and_every_entry_resolves():
    """The parametrised test above would pass on an empty list.

    Asserts the modules that MUST be on it rather than a count: a count pin
    expires the next time the list changes for a good reason, and this list has
    already changed once.
    """
    required = {"core/protocols.py", "core/vocabulary.py", "core/plugins.py"}
    assert required <= set(NEUTRAL), f"missing from the neutral list: {required - set(NEUTRAL)}"
    for relative in tuple(NEUTRAL) + MIXED:
        assert (SRC / relative).is_file(), relative


def test_the_mixed_modules_still_pass_the_vocabulary_check():
    """They are not neutral, but the vocabulary move DID reach them, and that
    part must not regress while the rest of the split is outstanding."""
    for relative in MIXED:
        leaked = _imports(SRC / relative) & VOCABULARY
        assert not leaked, f"{relative} imports {sorted(leaked)} again"


def test_the_vertical_still_does_import_it():
    """The mirror. If the vertical stopped importing the vocabulary, it would
    not be supplying one, and every test above would pass on a package that
    classifies nothing.

    Scoped to the vertical PACKAGE rather than one file. It named `bmc.py`
    alone until the field-strictness capability moved into `field_strictness.py`
    beside it and took `redfish_schema` along -- a true claim about how the
    vertical happened to be arranged, which is not the claim worth pinning.
    """
    across = set()
    for path in sorted((SRC / "verticals").rglob("*.py")):
        across |= _imports(path)
    missing = VOCABULARY - across
    assert not missing, (
        f"the vertical package no longer imports {sorted(missing)}, so it is "
        f"not supplying the vocabulary it exists to supply")


class TestTheReportSchemaDidNotMove:
    """The count keys were the reason this could not be done by deleting an
    import: they are emitted into a consumer's JSON."""

    def test_the_two_shipped_keys_are_still_produced(self):
        from bmc_sensor_audit.core import vocabulary
        keys = set(vocabulary.current().count_keys.values())
        assert {"not_a_sensor", "unrecognised_type"} <= keys, (
            "a key the shipped report has always carried is gone; that is a "
            "schema change under a consumer, not a refactor")


def _bmc_members_reached(path: Path) -> set[str]:
    """BMC-only members reached by ATTRIBUTE ACCESS.

    Not a word search. The measurement this replaces counted the word
    `undeclared` inside the report sentence "present, undeclared" and inside the
    neutral finding kind `undeclared_present`, and reported two interleavings in
    a module that had none.
    """
    reached = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Attribute) and node.attr in BMC_ONLY:
            reached.add(node.attr)
    return reached


@pytest.mark.parametrize("relative", NEUTRAL)
def test_a_neutral_module_reaches_no_bmc_only_member(relative):
    reached = _bmc_members_reached(SRC / relative)
    assert not reached, (
        f"{relative} reaches {sorted(reached)}. The other bridge has no "
        f"counterpart for these, so a module reaching one is in this domain "
        f"whether or not it imports the vocabulary")


def test_the_mixed_modules_are_mixed_for_a_reason():
    """The mirror again. If a MIXED module stopped reaching BMC-only members it
    belongs on the neutral list, and leaving it here would understate the work
    already done -- which is how `report.py` sat mislabelled for an iteration."""
    still_mixed = {rel for rel in MIXED if _bmc_members_reached(SRC / rel)}
    assert still_mixed == set(MIXED), (
        f"these are no longer mixed and should move to NEUTRAL: "
        f"{sorted(set(MIXED) - still_mixed)}")


#: The concrete BMC types. A neutral module importing one is typed on this
#: domain, whatever else it does or does not name.
CONCRETE_TYPES = {"redfish", "entity_manager"}


@pytest.mark.parametrize("relative", NEUTRAL)
def test_a_neutral_module_imports_no_bmc_type(relative):
    """The modules are typed on the protocols instead of this domain's classes.

    This became true without an adapter layer: `LiveSensor` already satisfied
    `CapturedPoint`, and the other three concrete types needed one alias each.
    The vertical conforms to the protocol; nothing wraps anything at runtime.
    """
    leaked = _imports(SRC / relative) & CONCRETE_TYPES
    assert not leaked, (
        f"{relative} imports {sorted(leaked)}. It is typed on this domain's "
        f"classes, so a second bridge cannot use it whatever else it avoids")


def test_the_vertical_still_owns_the_concrete_types():
    """The mirror. If nothing imported them, the types would have no home and
    this file would be passing over a package that captures nothing."""
    across = set()
    for path in sorted((SRC / "verticals").rglob("*.py")):
        across |= _imports(path)
    for path in sorted((SRC / "inventory").rglob("*.py")):
        across |= _imports(path)
    missing = CONCRETE_TYPES - across
    assert not missing, f"nothing imports {sorted(missing)} any more"
