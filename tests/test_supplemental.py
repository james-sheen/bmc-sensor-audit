"""Operator declarations, and the reason redundancy is not derived from the config.

The load-bearing tests here are the two refutations in
`TestRedundancyIsNotDerivableFromTheConfiguration`. They assert facts about the pinned
corpus that make auto-pairing wrong, so that if someone later reaches for the obvious
derivation -- same part, or same thresholds -- the evidence against it is already
executable rather than an argument in a docstring.

Everything else pins the file format's refusals. Each one is a hard error rather than a
warning, because a supplemental file that loads with a warning and creates no check
produces a run reporting no disagreements, which is indistinguishable from a board
where everything agrees.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "tests" / "fixtures" / "upstream"
sys.path.insert(0, str(ROOT / "src"))

from bmc_sensor_audit.detect.generator import (  # noqa: E402
    generate, pairing_candidates, peer_property)
from bmc_sensor_audit.detect.supplemental import (  # noqa: E402
    FORMAT, SupplementalError, load_supplemental, unmatched_names)
from bmc_sensor_audit.inventory.entity_manager import load_declaration  # noqa: E402


@pytest.fixture(scope="module")
def declaration():
    return load_declaration([str(UPSTREAM)])


def _write(tmp_path, **blocks):
    payload = {"format": FORMAT, "provenance": "test", **blocks}
    path = tmp_path / "supplemental.json"
    path.write_text(json.dumps(payload))
    return path


def _group(sensors, **rest):
    return {"sensors": list(sensors), "basis": "test fixture", **rest}


class TestRedundancyIsNotDerivableFromTheConfiguration:
    """Why this file exists at all, asserted against the pinned corpus.

    An expansion plan proposed auto-pairing channels of a multi-channel part. Both
    available derivations are refuted by the corpus itself, and these are the
    refutations -- kept executable so the argument cannot quietly rot into folklore
    while the code drifts back toward guessing.
    """

    def test_identical_thresholds_do_not_mean_redundant(self, declaration):
        """Six SLED sensors on six different parts carry the same four numbers. A
        rule pairing sensors by declared bounds would have made all six mutually
        redundant and reported a healthy chassis as inconsistent."""
        sleds = [s for s in declaration.sensors
                 if s.name.startswith("SLED") and "THERM" in s.name]
        assert len(sleds) >= 6, f"corpus changed: found {len(sleds)} SLED sensors"
        bounds = {tuple(sorted((t.bound, t.level, t.value) for t in s.thresholds))
                  for s in sleds}
        assert len(bounds) == 1, "the SLED sensors no longer share one bound set"
        assert len({s.part for s in sleds}) == len(sleds), (
            "the SLED sensors are no longer on distinct parts; the refutation this "
            "test carries depends on identical bounds spanning different hardware")

    def test_channels_of_one_part_are_not_the_same_measurement(self, declaration):
        """A TMP421 names a LOCAL and a REMOTE channel: its own die, and an external
        diode. They are one part and two measurements, which is exactly why sharing
        silicon cannot establish redundancy either."""
        pair = [s for s in declaration.sensors
                if s.name in ("MB_U73_THERM_LOCAL", "MB_U73_THERM_REMOTE")]
        assert len(pair) == 2, "the TMP421 specimen is no longer in the corpus"
        assert len({s.part for s in pair}) == 1, "they should be one part"
        assert {s.channel for s in pair} == {1, 2}
        assert "LOCAL" in pair[0].name or "LOCAL" in pair[1].name
        assert "REMOTE" in pair[0].name or "REMOTE" in pair[1].name


class TestCandidatesAreOfferedAndAssertedByNobody:
    def test_multi_channel_parts_are_listed(self, declaration):
        candidates = pairing_candidates(declaration)
        assert candidates, "the corpus carries multi-channel parts; none were listed"
        names = {tuple(c["channels"]) for c in candidates}
        assert ("MB_U73_THERM_LOCAL", "MB_U73_THERM_REMOTE") in names

    def test_a_candidate_never_becomes_a_pairing_on_its_own(self, declaration):
        """The whole distinction. Generating without a supplemental file must produce
        a model with no agreement check in it, however many candidates exist."""
        model, manifest = generate(declaration)
        assert manifest.candidates
        assert manifest.counts()["redundant_groups"] == 0
        for indicators in model["domain"]["indicators"].values():
            for indicator in indicators:
                assert "consistency" not in indicator
                assert "CONSISTENCY" not in indicator["axioms"]

    def test_a_single_channel_part_is_not_a_candidate(self, declaration):
        for candidate in pairing_candidates(declaration):
            assert len(candidate["channels"]) >= 2


class TestTheFileRefusesRatherThanWarns:
    def test_a_group_without_a_basis_is_refused(self, tmp_path):
        """`basis` is the difference between a specification and a guess."""
        path = _write(tmp_path, redundant_groups=[
            {"sensors": ["A", "B"]}])
        with pytest.raises(SupplementalError, match="basis"):
            load_supplemental(path)

    def test_a_group_of_one_is_refused(self, tmp_path):
        path = _write(tmp_path, redundant_groups=[_group(["A"])])
        with pytest.raises(SupplementalError, match="at least two"):
            load_supplemental(path)

    def test_a_group_naming_one_sensor_twice_is_refused(self, tmp_path):
        """A reading always agrees with itself, so the check would pass while
        measuring nothing -- a vacuous pass written into a config file."""
        path = _write(tmp_path, redundant_groups=[_group(["A", "A"])])
        with pytest.raises(SupplementalError, match="twice"):
            load_supplemental(path)

    def test_both_tolerances_at_once_is_refused(self, tmp_path):
        """The engine reads the absolute one and ignores the relative one, so the
        number written here would not be the number applied."""
        path = _write(tmp_path, redundant_groups=[
            _group(["A", "B"], tolerance=0.1, tolerance_absolute=2.0)])
        with pytest.raises(SupplementalError, match="both"):
            load_supplemental(path)

    def test_an_unknown_format_is_refused(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"format": "something/else"}))
        with pytest.raises(SupplementalError, match="format"):
            load_supplemental(path)

    def test_unparseable_json_is_refused(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text("{not json")
        with pytest.raises(SupplementalError, match="JSON"):
            load_supplemental(path)

    def test_a_counter_with_an_unknown_direction_is_refused(self, tmp_path):
        path = _write(tmp_path, counters=[
            {"sensor": "A", "basis": "t", "direction": "sideways"}])
        with pytest.raises(SupplementalError, match="direction"):
            load_supplemental(path)

    def test_a_well_formed_file_loads(self, tmp_path):
        path = _write(tmp_path,
                      redundant_groups=[_group(["A", "B"], tolerance=0.1)],
                      counters=[{"sensor": "C", "basis": "t"}])
        supplemental = load_supplemental(path)
        assert len(supplemental.redundant_groups) == 1
        assert supplemental.redundant_groups[0].primary == "A"
        assert supplemental.redundant_groups[0].peers == ("B",)
        assert supplemental.counters[0].direction == "increasing"


class TestANameThatMatchesNothingIsCaught:
    def test_a_typo_is_reported_against_the_declaration(self, tmp_path, declaration):
        """The silent failure this guards. A misspelled sensor creates no pairing at
        all, and the run then reports no disagreement because it asked no question."""
        path = _write(tmp_path, redundant_groups=[
            _group(["MB_U73_THERM_LOCAL", "MB_U73_THERM_REMOOTE"])])
        supplemental = load_supplemental(path)
        missing = unmatched_names(supplemental,
                                  {s.display_name for s in declaration.sensors})
        assert missing == ["MB_U73_THERM_REMOOTE"]

    def test_correct_names_produce_no_complaint(self, tmp_path, declaration):
        path = _write(tmp_path, redundant_groups=[
            _group(["MB_U73_THERM_LOCAL", "MB_U73_THERM_REMOTE"])])
        supplemental = load_supplemental(path)
        assert unmatched_names(
            supplemental, {s.display_name for s in declaration.sensors}) == []


class TestTheGeneratedModelCarriesTheDeclaration:
    @pytest.fixture
    def built(self, tmp_path, declaration):
        path = _write(tmp_path, redundant_groups=[
            _group(["MB_U73_THERM_LOCAL", "MB_U73_THERM_REMOTE"], tolerance=0.10)])
        model, manifest = generate(declaration,
                                   supplemental=load_supplemental(path))
        return model, manifest

    def test_the_primary_carries_the_block_and_the_peer_does_not(self, built):
        """One side declares it. The engine's test is symmetric, so declaring it on
        both would produce two findings for one disagreement."""
        model, manifest = built
        primary = manifest.type_for("MB_U73_THERM_LOCAL")
        peer = manifest.type_for("MB_U73_THERM_REMOTE")
        assert "consistency" in model["domain"]["indicators"][primary][0]
        assert "consistency" not in model["domain"]["indicators"][peer][0]

    def test_consistency_arrives_with_a_rule_the_engine_can_run(self, built):
        """CONSISTENCY asks two different questions and this generator only ever
        asks one of them, which is why it declares no `role:` anywhere.

        Reported from outside 2026-09-02 as a coming coverage drop: the engine
        stopped inferring a CONSISTENCY role from an indicator's NAME, this
        package declares no roles, so every CONSISTENCY cell would begin
        declining `missing_role`. Measured against the engine before believing
        it, and the premise is false -- the axiom is emitted in ONE place and
        only together with the `agrees_with` block below, and the agreement rule
        needs no role. What declines is CONSISTENCY carrying neither.

        DECLARING ROLES WOULD HAVE BEEN THE WRONG REMEDY, and worse than doing
        nothing. Nothing here knows what kind of quantity a BMC sensor reports,
        so the only way to pick a role per sensor is to read its NAME -- which is
        the inference the engine just removed. The answer to a name-derived rule
        is never a hand-written copy of the same guess.

        The residue is this pin rather than a change: it holds the property that
        makes roles unnecessary, and fails if a later edit declares CONSISTENCY
        without a rule. That failure would otherwise be silent, because a decline
        reads as coverage the reader never had.
        """
        model, _ = built
        declared = [(t, i) for t, inds in model["domain"]["indicators"].items()
                    for i in inds if "CONSISTENCY" in i.get("axioms", [])]
        assert declared, "no CONSISTENCY was generated; this pin is vacuous"
        naked = [f"{t}.{i['name']}" for t, i in declared
                 if "consistency" not in i and "role" not in i]
        assert naked == [], (
            f"{naked} declare CONSISTENCY with neither an `agrees_with` block "
            f"nor a `role:`; the engine declines those and the coverage is "
            f"imaginary")

    def test_the_peer_is_named_as_a_property_not_an_entity(self, built):
        """A checker holds an IndicatorSpec and an Entity and never the model, so it
        cannot resolve another entity's indicator. `agrees_with` names properties."""
        model, manifest = built
        primary = manifest.type_for("MB_U73_THERM_LOCAL")
        block = model["domain"]["indicators"][primary][0]["consistency"]
        assert block["agrees_with"] == [peer_property("MB_U73_THERM_REMOTE")]
        assert block["tolerance"] == 0.10

    def test_the_declared_tolerance_is_the_one_emitted(self, tmp_path, declaration):
        path = _write(tmp_path, redundant_groups=[
            _group(["MB_U73_THERM_LOCAL", "MB_U73_THERM_REMOTE"],
                   tolerance_absolute=3.0)])
        model, manifest = generate(declaration,
                                   supplemental=load_supplemental(path))
        block = model["domain"]["indicators"][
            manifest.type_for("MB_U73_THERM_LOCAL")][0]["consistency"]
        assert block == {"agrees_with": [peer_property("MB_U73_THERM_REMOTE")],
                         "tolerance_absolute": 3.0}
        assert "tolerance" not in block

    def test_the_manifest_records_the_pairing_and_its_source(self, built):
        model, manifest = built
        sensor = next(s for s in manifest.sensors
                      if s.declared_name == "MB_U73_THERM_LOCAL")
        assert sensor.agrees_with == ("MB_U73_THERM_REMOTE",)
        assert manifest.counts()["redundant_groups"] == 1
        assert manifest.to_dict()["supplemental_source"] == "supplemental.json"

    def test_the_manifest_emits_no_absolute_path(self, built):
        """A manifest is an artifact somebody commits, and the supplemental file's
        path is a path under whoever ran the tool's home directory."""
        _, manifest = built
        assert "/" not in (manifest.to_dict()["supplemental_source"] or "")
