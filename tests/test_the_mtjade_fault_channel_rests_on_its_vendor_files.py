"""The Mt. Jade fault channel: the reverse of the coupling, and no stronger than it.

A coupling says which way the firmware drives a value; a fault channel says which
way a FAILURE travels, and on this board they run opposite ways. The fan-control
files set FAN3 from TS4_Temp; a fan that stops cannot cool its zone, so a failure
of the fan is one way that reading can move. The channel rests on the same zone
assignment the coupling cites, and each clause of its basis is read back out of
the vendored files here, as the coupling's are.

WHAT IT DOES NOT SAY is how strongly. That is airflow physics no file here
states, and the same files record TS4_Temp as the zone's ambient reading -- one a
fan barely moves. So no weight is declared, and the engine's answer to *what could
explain this reading* names the fan and ranks nothing: `cpt_missing`.
"""

from __future__ import annotations

from presence_audit import generator
from presence_audit.generator import generate
from presence_audit.supplemental import load_supplemental, unmatched_names
from bmc_sensor_audit.inventory.entity_manager import load_declaration

from test_the_mtjade_coupling_rests_on_its_vendor_files import (
    EXAMPLE, MTJADE, _upstream_json)

SOURCE, TARGET = "FAN3_1", "TS4_Temp"


def _channel():
    channels = load_supplemental(str(EXAMPLE)).fault_channels
    assert len(channels) == 1, channels
    return channels[0]


class TestItIsTheCouplingReversed:

    def test_it_runs_from_the_fan_to_the_reading_that_sets_it(self):
        coupling = load_supplemental(str(EXAMPLE)).couplings[0]
        channel = _channel()
        assert (channel.source, channel.target) == (SOURCE, TARGET)
        assert (channel.source, channel.target) == (coupling.target, coupling.source)


class TestEveryClauseOfTheBasisIsInTheFiles:

    def test_the_fan_and_the_reading_share_a_zone(self):
        groups = {g["name"]: g["members"] for g in _upstream_json("groups.json")}
        fan = SOURCE.split("_")[0]
        assert any(m.endswith(f"/{fan}") for m in groups["air_cooled_zone0_fans"])
        assert f"/xyz/openbmc_project/sensors/temperature/{TARGET}" in groups["zone0_ambient"]

    def test_the_basis_names_the_groups_the_files_hold(self):
        basis = _channel().basis
        assert "air_cooled_zone0_fans" in basis and "zone0_ambient" in basis

    def test_it_says_the_reading_is_the_ambient_one_and_claims_no_strength(self):
        channel = _channel()
        assert "AMBIENT" in channel.basis
        assert channel.weight is None and channel.weight_basis is None


class TestItRunsAgainstThePlatform:

    def test_both_names_are_declared_by_the_vendored_configuration(self):
        declaration = load_declaration([str(MTJADE)])
        names = {s.display_name for s in declaration.sensors}
        assert unmatched_names(load_supplemental(str(EXAMPLE)), names) == []

    def test_it_generates_a_causal_rule_with_no_strength(self):
        declaration = load_declaration([str(MTJADE)])
        model, manifest = generate(declaration, domain_id="bmc-sensor-audit",
                                   supplemental=load_supplemental(str(EXAMPLE)))
        assert manifest.channeled == [(SOURCE, TARGET)]
        rules = [r for r in model["domain"]["relationship_rules"]
                 if r.get("edge_direction") == "causal"]
        assert rules == [{"type": generator.FAULT_RELATION,
                          "source_type": manifest.type_for(SOURCE),
                          "target_type": manifest.type_for(TARGET),
                          "edge_direction": "causal"}]
