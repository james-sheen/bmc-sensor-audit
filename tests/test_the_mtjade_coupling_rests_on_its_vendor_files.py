"""The Mt. Jade coupling states a basis, and every clause of it is read back here.

`examples/supplemental/ampere-mtjade.json` declared no coupling for three
releases, and four searches for a document that could justify one came back
empty -- all of them against a design-document tree. The platform's own
**phosphor-fan-control** configuration was in reach the whole time, and it is
the vendor saying in its firmware which reading sets which fans. It is vendored
here, pinned, and the coupling cites it.

A basis written as prose is a claim that can drift from its source without
anything going red. So each factual clause -- which group a sensor is in, what
the map does, the zone's timing -- is DERIVED from the vendored files and
checked against the declaration, in both directions: the numbers the basis
states must be the numbers the files hold.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from presence_audit.generator import generate
from presence_audit.supplemental import load_supplemental, unmatched_names
from bmc_sensor_audit.inventory.entity_manager import load_declaration

ROOT = Path(__file__).resolve().parents[1]
FAN_CONTROL = ROOT / "tests" / "fixtures" / "fan-control" / "ampere-mtjade"
MTJADE = ROOT / "tests" / "fixtures" / "upstream" / "ampere" / "mtjade.json"
EXAMPLE = ROOT / "examples" / "supplemental" / "ampere-mtjade.json"
PIN = "9a2f15583ae35eeb06852b4f825b440653323338"

DRIVER, DRIVEN = "TS4_Temp", "FAN3_1"


def _upstream_json(name: str):
    """phosphor-fan-control reads JSON with `//` comments and trailing commas,
    so its configuration is not strict JSON. Read it the way upstream does
    rather than editing a vendored file into shape."""
    text = (FAN_CONTROL / name).read_text()
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    text = re.sub(r",(\s*[\]}])", r"\1", text)
    return json.loads(text)


def _git_blob(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@pytest.fixture(scope="module")
def coupling():
    couplings = load_supplemental(str(EXAMPLE)).couplings
    assert len(couplings) == 1, couplings
    return couplings[0]


@pytest.fixture(scope="module")
def ts_mapping():
    events = _upstream_json("events.json")
    event = next(e for e in events if e["name"] == "target_mapping_from_TS_temp")
    return event["actions"][0]


class TestTheVendoredFilesAreWhatUpstreamShips:

    @pytest.mark.parametrize("name", ["events.json", "zones.json", "groups.json"])
    def test_byte_identical_to_the_blob_the_readme_names(self, name):
        readme = (FAN_CONTROL / "README.md").read_text()
        row = re.search(rf"`{re.escape(name)}` \| `([0-9a-f]{{40}})`", readme)
        assert row, f"the README names no blob for {name}"
        assert _git_blob(FAN_CONTROL / name) == row.group(1)

    def test_the_readme_and_the_notice_name_one_pin(self):
        assert PIN in (FAN_CONTROL / "README.md").read_text()
        assert PIN in (ROOT / "NOTICE").read_text()

    def test_the_layers_own_licensing_travels_with_them(self):
        statement = (FAN_CONTROL / "LICENSE").read_text()
        assert "MIT" in statement and "Apache-2.0" in statement
        assert "Apache License" in (FAN_CONTROL / "COPYING.apache-2.0").read_text()
        assert "Permission is hereby granted" in (FAN_CONTROL / "COPYING.MIT").read_text()


class TestEveryClauseOfTheBasisIsInTheFiles:

    def test_the_driver_is_the_zones_ambient_reading(self):
        groups = {g["name"]: g["members"] for g in _upstream_json("groups.json")}
        assert f"/xyz/openbmc_project/sensors/temperature/{DRIVER}" in groups["zone0_ambient"]

    def test_the_driven_fan_is_in_the_zone(self):
        groups = {g["name"]: g["members"] for g in _upstream_json("groups.json")}
        fan = DRIVEN.split("_")[0]
        assert any(m.endswith(f"/{fan}") for m in groups["air_cooled_zone0_fans"])

    def test_the_mapping_reads_the_ambient_group(self, ts_mapping):
        assert ts_mapping["name"] == "target_from_group_max"
        assert [g["name"] for g in ts_mapping["groups"]] == ["zone0_ambient"]

    def test_the_map_never_decreases(self, ts_mapping):
        targets = [point["target"] for point in ts_mapping["map"]]
        assert all(a <= b for a, b in zip(targets, targets[1:])), targets

    def test_the_basis_quotes_the_maps_ends_as_the_file_holds_them(
            self, coupling, ts_mapping):
        first, last = ts_mapping["map"][0], ts_mapping["map"][-1]
        assert f"{first['target']:g} of 255 at {first['value']:g} C" in coupling.basis
        assert f"{last['target']:g} of 255 at {last['value']:g} C" in coupling.basis

    def test_five_more_mappings_drive_the_same_zone(self, coupling):
        mappings = [e for e in _upstream_json("events.json")
                    if any(a.get("name") == "target_from_group_max"
                           for a in e.get("actions", []))]
        assert len(mappings) == 6, [e["name"] for e in mappings]
        assert "five more mappings" in coupling.basis

    def test_the_zones_timing_is_the_one_the_basis_states(self, coupling):
        zone = _upstream_json("zones.json")[0]
        assert f"increases to {zone['increase_delay']} s" in coupling.basis
        assert f"decreases to {zone['decrease_interval']} s" in coupling.basis


class TestTheDeclarationFollowsFromTheTiming:
    """A step with no delay is the right shape only when the whole response
    lands inside one collection step -- which is a claim about the cadence."""

    def test_the_cadence_is_no_faster_than_the_slower_throttle(self):
        zone = _upstream_json("zones.json")[0]
        cadence = load_supplemental(str(EXAMPLE)).sampling_interval_s
        assert cadence >= zone["decrease_interval"], cadence

    def test_it_is_a_step_with_no_delay(self, coupling):
        assert coupling.response_model == "step"
        assert coupling.propagation_delay_s == 0

    def test_the_time_constant_is_the_slower_throttle(self, coupling):
        """Inert under a step, and so it is a documented number rather than a
        chosen one."""
        zone = _upstream_json("zones.json")[0]
        assert coupling.time_constant_s == zone["decrease_interval"]

    def test_the_gain_is_withheld(self, coupling):
        """The map is in PWM counts and the reading is RPM; the fan curve
        between them is in none of these files."""
        assert coupling.gain == "estimate"
        assert coupling.gain_basis is None


class TestItRunsAgainstThePlatform:

    def test_both_names_are_declared_by_the_vendored_configuration(self):
        declaration = load_declaration([str(MTJADE)])
        names = {s.display_name for s in declaration.sensors}
        assert unmatched_names(load_supplemental(str(EXAMPLE)), names) == []

    def test_it_generates_the_coupling_with_the_gain_withheld(self):
        declaration = load_declaration([str(MTJADE)])
        model, manifest = generate(declaration, domain_id="bmc-sensor-audit",
                                   supplemental=load_supplemental(str(EXAMPLE)))
        assert (DRIVER, DRIVEN) in manifest.coupled
        rules = [r for r in model["domain"].get("relationship_rules", [])
                 if r.get("type") == "drives"]
        assert len(rules) == 1, rules
        gain = (rules[0].get("transition") or {}).get("gain")
        assert not isinstance(gain, (int, float)), (
            f"a withheld gain was generated as the number {gain!r}")
