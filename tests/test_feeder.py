"""The feeder and the gate, tested without the engine.

`feed` needs only something shaped like a session, and `evaluate` takes plain
dictionaries, so both are exercised here with no optional extra installed. The real
end-to-end — generated model, real engine, real envelope — lives in
`test_engine_bridge.py`, which skips when the extra is absent.

**The test that matters most is `test_a_data_sufficiency_decline_does_not_fail_the_gate`
paired with `test_a_core_case_decline_fails_the_gate`.** Those two are the whole exit
code contract: *could not evaluate yet* must read as neither *sensors are missing* nor
*all clear*, and the difference between them is the reason this project exists.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "tests" / "fixtures" / "upstream"
sys.path.insert(0, str(ROOT / "src"))

from bmc_sensor_audit.detect.feeder import (  # noqa: E402
    STUCK_AT_SAMPLE_FLOOR, evaluate, feed, unmapped_observations)
from bmc_sensor_audit.detect.generator import (  # noqa: E402
    READING, READING_LOW, generate)
from bmc_sensor_audit.inventory.diff import compare  # noqa: E402
from bmc_sensor_audit.inventory.entity_manager import load_declaration  # noqa: E402
from bmc_sensor_audit.inventory.redfish import walk_from_dict  # noqa: E402


class StubSession:
    """Everything the feeder is allowed to assume about an EngineSession."""

    def __init__(self):
        self.entities: dict[str, tuple] = {}
        self.observations: list[tuple] = []

    def add_entity(self, entity_id, entity_type, properties=None):
        self.entities[entity_id] = (entity_type, dict(properties or {}))

    def add_observations(self, entity_id, prop, values, interval_seconds=60.0):
        self.observations.append((entity_id, prop, list(values)))


@pytest.fixture(scope="module")
def built():
    declaration = load_declaration([str(UPSTREAM)])
    model, manifest = generate(declaration)
    return declaration, model, manifest


def _walk(entries):
    return walk_from_dict({
        "format": "bmc-sensor-audit/walk/1", "chassis": ["/redfish/v1/Chassis/1"],
        "shapes_seen": ["sensors"], "errors": [],
        "sensors": [{"name": name, "path": f"/redfish/v1/Chassis/1/Sensors/s{i}",
                     "reading": reading, "state": state, "health": "OK",
                     "thresholds": {}}
                    for i, (name, reading, state) in enumerate(entries)]})


def _report(declaration, entries):
    return compare(declaration, _walk(entries))


class TestOnlyPresentAndReadingIsFed:
    """The layering rule. Absence is a question Stage 1 answers precisely, with a
    three-valued verdict the engine has no equivalent for."""

    def test_a_reading_sensor_is_fed(self, built):
        declaration, _, manifest = built
        sensor = manifest.sensors[0]
        session = StubSession()
        result = feed(session, manifest,
                      [_report(declaration, [(sensor.declared_name, 1.0, "Enabled")])])
        assert result.fed == 1
        assert sensor.entity_type in session.entities

    def test_a_present_but_disabled_sensor_is_not_fed(self, built):
        declaration, _, manifest = built
        sensor = manifest.sensors[0]
        session = StubSession()
        result = feed(session, manifest,
                      [_report(declaration, [(sensor.declared_name, 1.0, "Disabled")])])
        assert result.fed == 0
        assert result.skipped_not_reading == 1
        assert session.entities == {}

    def test_the_lower_bound_indicator_is_fed_negated(self, built):
        """The negation has to happen on the way in as well as in the model, or the
        indicator the model declares is never supplied and declines forever."""
        declaration, _, manifest = built
        sensor = next(s for s in manifest.sensors if s.has_lower)
        session = StubSession()
        feed(session, manifest,
             [_report(declaration, [(sensor.declared_name, 2.5, "Enabled")])])
        _, properties = session.entities[sensor.entity_type]
        assert properties[READING] == 2.5
        assert properties[READING_LOW] == -2.5

    def test_a_sensor_with_no_lower_bound_gets_no_negated_property(self, built):
        declaration, _, manifest = built
        sensor = next((s for s in manifest.sensors if not s.has_lower), None)
        if sensor is None:
            pytest.skip("every vendored sensor happens to declare a lower bound")
        session = StubSession()
        feed(session, manifest,
             [_report(declaration, [(sensor.declared_name, 2.5, "Enabled")])])
        _, properties = session.entities[sensor.entity_type]
        assert READING_LOW not in properties


class TestLivenessWarmsUpAndSaysSo:
    def test_one_walk_is_one_sample(self, built):
        declaration, _, manifest = built
        sensor = manifest.sensors[0]
        session = StubSession()
        result = feed(session, manifest,
                      [_report(declaration, [(sensor.declared_name, 1.0, "Enabled")])])
        assert result.samples[sensor.declared_name] == 1

    def test_history_accumulates_across_walks(self, built):
        declaration, _, manifest = built
        sensor = manifest.sensors[0]
        reports = [_report(declaration, [(sensor.declared_name, 1.0 + i, "Enabled")])
                   for i in range(12)]
        session = StubSession()
        result = feed(session, manifest, reports)
        assert result.samples[sensor.declared_name] == 12
        series = [o for o in session.observations
                  if o[0] == sensor.entity_type and o[1] == READING][0][2]
        assert series == [1.0 + i for i in range(12)]

    def test_warming_up_is_visible_below_the_floor(self, built):
        """A tool that hid this would look like it was checking liveness from the
        first walk, which is the vacuous pass this project finds in other systems."""
        declaration, _, manifest = built
        sensor = manifest.sensors[0]
        session = StubSession()
        result = feed(session, manifest,
                      [_report(declaration, [(sensor.declared_name, 1.0, "Enabled")])])
        assert result.warming_up == {sensor.declared_name: 1}

    def test_warming_up_clears_at_the_floor(self, built):
        declaration, _, manifest = built
        sensor = manifest.sensors[0]
        reports = [_report(declaration, [(sensor.declared_name, 1.0 + i, "Enabled")])
                   for i in range(STUCK_AT_SAMPLE_FLOOR)]
        session = StubSession()
        assert feed(session, manifest, reports).warming_up == {}


class TestTheExitCodeContract:
    """*Could not evaluate* must read as neither *sensors are missing* nor *all
    clear*. These tests are that sentence."""

    def _outcome(self, manifest, *, findings=(), declines=(), describe=None, strict=False):
        envelope = {"checked": {"invariants": 1, "entities": 1},
                    "findings": list(findings), "not_checked": list(declines)}
        return evaluate(envelope, describe or {}, manifest, strict_declines=strict)

    def test_a_clean_envelope_passes(self, built):
        _, _, manifest = built
        assert self._outcome(manifest).exit_code == 0

    def test_a_finding_fails_the_gate(self, built):
        _, _, manifest = built
        sensor = manifest.sensors[0]
        outcome = self._outcome(manifest, findings=[{
            "entity_id": sensor.entity_type, "severity": "critical",
            "problem_type": f"threshold_exceeded:{READING}",
            "reason": "reading exceeds critical threshold"}])
        assert outcome.exit_code == 1
        assert sensor.declared_name in outcome.findings[0]

    def test_a_core_case_decline_fails_the_gate(self, built):
        """Stage 1 said this sensor was reading. If its value did not reach the
        model, the mapping is wrong — and a mapping error is invisible unless
        something fails on it."""
        _, _, manifest = built
        outcome = self._outcome(manifest, declines=[
            {"entity_id": "x", "axiom": "BOUNDEDNESS", "reason": "missing_property"}])
        assert outcome.core_case_declines
        assert outcome.exit_code == 1

    def test_a_data_sufficiency_decline_does_not_fail_the_gate(self, built):
        """Not enough history yet is honest and not a regression. Failing here would
        make every first walk red, and a gate that is always red is not read."""
        _, _, manifest = built
        outcome = self._outcome(manifest, declines=[
            {"entity_id": "x", "axiom": "STABILITY", "reason": "insufficient_samples"}])
        assert outcome.data_declines
        assert outcome.exit_code == 0

    def test_strict_declines_escalates_the_sufficiency_class(self, built):
        _, _, manifest = built
        outcome = self._outcome(manifest, declines=[
            {"entity_id": "x", "axiom": "STABILITY", "reason": "insufficient_samples"}],
            strict=True)
        assert outcome.exit_code == 1

    def test_an_unknown_reason_is_neither_bucket(self, built):
        """The engine does not export its decline vocabulary, so an unrecognised
        reason is a certainty over time rather than a hypothetical. Folding it into
        the nearest known bucket would reclassify the case and report it with
        confidence."""
        _, _, manifest = built
        outcome = self._outcome(manifest, declines=[
            {"entity_id": "x", "axiom": "STABILITY", "reason": "some_new_reason"}])
        assert outcome.unclassified_declines
        assert not outcome.core_case_declines and not outcome.data_declines
        assert outcome.exit_code == 0, "an unknown reason must not silently fail a gate"
        assert self._outcome(manifest, declines=[
            {"entity_id": "x", "axiom": "STABILITY", "reason": "some_new_reason"}],
            strict=True).exit_code == 1

    def test_an_unmapped_observation_fails_the_gate(self, built):
        """AC3. A reading fed under a name the model does not declare is a sensor the
        gate silently stopped watching."""
        _, _, manifest = built
        describe = {"unconsumed_observations": [
            {"entity_id": "x", "property": "raeding", "observations": 3,
             "reason": "undeclared_property"}]}
        outcome = self._outcome(manifest, describe=describe)
        assert outcome.unmapped
        assert outcome.exit_code == 1

    def test_two_is_never_returned_from_here(self, built):
        """`2` means could-not-complete and belongs to the caller, which knows
        whether the BMC answered. Conflating it with *sensors are missing* fails a
        good firmware image."""
        _, _, manifest = built
        for kwargs in ({}, {"findings": [{"entity_id": "x", "problem_type": "p:reading"}]},
                       {"declines": [{"reason": "missing_property"}]}):
            assert self._outcome(manifest, **kwargs).exit_code in (0, 1)


class TestTheUnmappedReaderToleratesRelocation:
    def test_it_reads_the_measured_top_level_location(self):
        payload = {"unconsumed_observations": [{"property": "a"}], "model": {}}
        assert unmapped_observations(payload) == [{"property": "a"}]

    def test_it_also_finds_the_key_under_model(self):
        """If the key relocates, this keeps working and the canary fails loudly —
        which is the right way round. Silent blindness here means every mapping
        error stops being visible."""
        payload = {"model": {"unconsumed_observations": [{"property": "b"}]}}
        assert unmapped_observations(payload) == [{"property": "b"}]

    def test_absent_means_empty_not_an_error(self):
        assert unmapped_observations({"model": {}}) == []
