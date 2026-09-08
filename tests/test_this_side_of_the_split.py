"""What this package owes the neutral half, now that the neutral half has left.

`test_neutral_modules_carry_no_vocabulary.py` used to ask which of THIS
package's modules were neutral. That question no longer has a subject here: the
neutral modules are `presence-audit`, a separate distribution with no
dependencies, and it asserts its own neutrality structurally rather than against
a hand-kept list. The list this file replaced had been corrected in both
directions twice, which is what a hand-kept list does.

What survives is the MIRROR, and it is the half that could never be replaced by
anything in the other package: if this vertical stopped supplying a vocabulary,
or nothing here owned the concrete types any more, then `presence-audit` would
be neutral over a domain that had quietly stopped existing -- and every
assertion on that side would still pass.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "bmc_sensor_audit"

#: What supplying a vocabulary looks like from this side.
VOCABULARY = {"vocabulary", "sensor_types"}

#: The types only this domain has. A capture and a declaration are BMC objects
#: here; the protocols they satisfy live in the other package.
CONCRETE_TYPES = {"redfish", "entity_manager"}


def _imports(path):
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[-1] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module.split(".")[-1])
            out.update(a.name for a in node.names)
    return out


def _across(*dirs):
    seen = set()
    for d in dirs:
        for path in sorted((SRC / d).rglob("*.py")):
            seen |= _imports(path)
    return seen


def test_there_are_modules_to_scan():
    """Non-vacuity: both assertions below quantify over a directory walk, and a
    walk that returns nothing makes them true and meaningless."""
    assert list((SRC / "verticals").rglob("*.py"))
    assert list((SRC / "inventory").rglob("*.py"))


def test_the_vertical_still_supplies_a_vocabulary():
    """If it stopped, `presence-audit` would classify nothing and report cleanly
    -- and nothing on that side could tell, because a core with no vertical is
    exactly what that package is designed to be."""
    missing = VOCABULARY - _across("verticals")
    assert not missing, (
        f"the vertical package no longer imports {sorted(missing)}, so it is "
        f"not supplying the vocabulary it exists to supply")


def test_this_side_still_owns_the_concrete_types():
    """The protocols moved; the objects that satisfy them did not."""
    missing = CONCRETE_TYPES - _across("verticals", "inventory")
    assert not missing, f"nothing imports {sorted(missing)} any more"


def test_the_neutral_half_is_a_dependency_and_not_a_copy():
    """The failure this split exists to prevent, and the one that would be
    invisible: both trees defining the same module, drifting apart."""
    for gone in ("core", "detect"):
        assert not (SRC / gone).exists(), (
            f"src/bmc_sensor_audit/{gone}/ is back. The neutral modules live in "
            f"presence-audit; a copy here would be the one that goes stale")
    for gone in ("report.py", "inventory/diff.py", "inventory/regression.py"):
        assert not (SRC / gone).exists(), f"{gone} is back as a local copy"
