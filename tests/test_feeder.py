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
    ENVELOPE_SCHEMA_VERSION, STUCK_AT_SAMPLE_FLOOR, evaluate, feed,
    unmapped_observations)
from bmc_sensor_audit.detect.generator import READING, generate  # noqa: E402
from bmc_sensor_audit.inventory.diff import compare  # noqa: E402
from bmc_sensor_audit.inventory.entity_manager import load_declaration  # noqa: E402
from bmc_sensor_audit.inventory.redfish import walk_from_dict  # noqa: E402

# A sentinel distinct from every value the field could legitimately take,
# including None -- `None` is one of the wrong versions under test.
_ABSENT = object()


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

    def test_a_lower_bounded_sensor_is_fed_its_reading_verbatim(self, built):
        """These two used to assert that a lower-bounded sensor was ALSO fed a
        mirrored `-value` property, because BOUNDEDNESS could only test upward.
        The engine takes floors natively now, so what has to be true is that the
        reading arrives unmodified and nothing else arrives beside it -- a
        surviving mirror would feed a value no indicator reads."""
        declaration, _, manifest = built
        sensor = next(s for s in manifest.sensors if s.has_lower)
        session = StubSession()
        feed(session, manifest,
             [_report(declaration, [(sensor.declared_name, 2.5, "Enabled")])])
        _, properties = session.entities[sensor.entity_type]
        assert properties == {READING: 2.5}

    def test_a_lower_bounded_sensor_gets_one_observation_series(self, built):
        """The mirror had a second half: a negated series fed alongside the real
        one. Two series where the model declares one indicator is an unread feed,
        and the engine reports those only if something asks."""
        declaration, _, manifest = built
        sensor = next(s for s in manifest.sensors if s.has_lower)
        session = StubSession()
        feed(session, manifest,
             [_report(declaration, [(sensor.declared_name, v, "Enabled")])
              for v in (2.5, 2.6, 2.7)])
        fed = [call for call in session.observations
               if call[0] == sensor.entity_type]
        assert len(fed) == 1, f"expected one series, got {[c[1] for c in fed]}"
        assert fed[0][1] == READING
        assert fed[0][2] == [2.5, 2.6, 2.7]


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


class TestTheDetectCommandComposesTwoStages:
    """The CLI surface. These run with no engine installed, which is exactly the case
    worth pinning: a tool that crashes when its optional extra is absent has made the
    extra mandatory by accident."""

    @staticmethod
    def _run(tmp_path, *, walks=1, threshold_in_walk=True):
        import subprocess
        config = tmp_path / "board.json"
        config.write_text(json.dumps({"Name": "B", "Exposes": [
            {"Name": "Inlet Temp", "Type": "TMP75", "Thresholds": [
                {"Name": "upper critical", "Direction": "greater than",
                 "Value": 80, "Severity": 1}]}]}))
        argv = [sys.executable, "-m", "bmc_sensor_audit.cli", "detect",
                "--config", str(config)]
        for index in range(walks):
            walk = tmp_path / f"w{index}.json"
            walk.write_text(json.dumps({
                "format": "bmc-sensor-audit/walk/1", "chassis": ["/c/1"],
                "shapes_seen": ["sensors"], "errors": [],
                "sensors": [{"name": "Inlet Temp", "path": "/c/1/S/0",
                             "reading": 22.5 + index, "state": "Enabled",
                             "health": "OK",
                             "thresholds": {"upper/critical": 80.0}
                             if threshold_in_walk else {}}]}))
            argv += ["--walk", str(walk)]
        import os
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run(argv, cwd=str(tmp_path), capture_output=True,
                              text=True, env=env)

    def test_stage_one_still_runs_and_reports_without_the_engine(self, tmp_path):
        result = self._run(tmp_path)
        assert "declared" in result.stdout, result.stderr
        assert "Sensor coverage" in result.stdout

    def test_a_missing_extra_is_could_not_complete_not_a_failure(self, tmp_path):
        """`2`, not `1`. A pipeline that reads *the engine is not installed* as
        *sensors are missing* fails a good firmware image."""
        pytest.importorskip
        try:
            import arbiter_engine  # noqa: F401
            pytest.skip("the extra is installed; this pins the absent case")
        except ImportError:
            pass
        result = self._run(tmp_path)
        assert result.returncode == 2, result.stdout[-400:]
        assert "pip install" in result.stderr, result.stderr

    def test_the_install_hint_names_the_extra_by_name(self, tmp_path):
        try:
            import arbiter_engine  # noqa: F401
            pytest.skip("the extra is installed")
        except ImportError:
            pass
        result = self._run(tmp_path)
        assert "[detect]" in result.stderr

    def test_stage_one_coverage_is_stated_as_unaffected(self, tmp_path):
        """A reader who sees a `2` needs to know which half of the run produced it."""
        try:
            import arbiter_engine  # noqa: F401
            pytest.skip("the extra is installed")
        except ImportError:
            pass
        result = self._run(tmp_path)
        assert "unaffected" in result.stderr


class TestTheEnvelopeSchemaVersionIsChecked:
    """The wire contract is versioned separately from the package, and everything
    this module reads is keyed to it.

    Nothing consumed `meta.schema_version` before: the field shipped, was described
    in the engine's compatibility notes, and the one consumer that parses the
    envelope by hand ignored it. A shape change would then have arrived as findings
    filed under the wrong side of the band and declines landing in `unclassified`,
    which reads like a bad board rather than a moved contract.
    """

    def _envelope(self, version, **rest):
        envelope = {"checked": {}, "findings": [], "not_checked": [], **rest}
        if version is not _ABSENT:
            envelope["meta"] = {"schema_version": version, "source": "live"}
        return envelope

    def test_the_supported_version_passes_silently(self, built):
        _, _, manifest = built
        outcome = evaluate(self._envelope(ENVELOPE_SCHEMA_VERSION), {}, manifest)
        assert outcome.schema_mismatch is None

    @pytest.mark.parametrize("version", [2, 0, "1", 1.5, None])
    def test_any_other_version_is_refused_and_names_both_numbers(self, built, version):
        """Including `"1"` and `1.5`. A string that looks like the right number is
        the case a `!=` comparison gets right and an `int()` coercion gets wrong."""
        _, _, manifest = built
        outcome = evaluate(self._envelope(version), {}, manifest)
        assert outcome.schema_mismatch is not None
        assert str(version) in outcome.schema_mismatch
        assert str(ENVELOPE_SCHEMA_VERSION) in outcome.schema_mismatch

    def test_an_absent_version_is_not_a_mismatch(self, built):
        """Absent is not wrong. Engines before 0.1.7 predate the field entirely, and
        the pin has since been re-derived past them -- but the rule is about the
        SHAPE, not about which releases are admitted today: reading a missing key as
        *unsupported* rather than *empty* is a mistake this repository has already
        been caught by once, and tightening a pin does not stop it being one."""
        _, _, manifest = built
        outcome = evaluate(self._envelope(_ABSENT), {}, manifest)
        assert outcome.schema_mismatch is None

    def test_a_mismatch_does_not_silently_suppress_the_findings(self, built):
        """Reported alongside, not instead. Dropping the findings would leave an
        operator with a warning and no evidence; keeping them unlabelled would
        present a reading taken through the wrong contract as a verdict."""
        _, _, manifest = built
        sensor = manifest.sensors[0]
        envelope = self._envelope(99, findings=[
            {"entity_id": sensor.entity_type, "severity": "critical",
             "problem_type": "threshold_exceeded:reading",
             "reason": "reading exceeds critical threshold"}])
        outcome = evaluate(envelope, {}, manifest)
        assert outcome.schema_mismatch is not None
        assert len(outcome.findings) == 1

    def test_the_real_engine_stamps_the_version_this_build_expects(self):
        """The one that would catch an engine bump inside the pin moving the shape.

        Asserted against a live envelope rather than the constant, because a
        constant agreeing with itself is not a check.
        """
        pytest.importorskip("arbiter_engine.api",
                            reason="engine is the optional [detect] extra")
        import tempfile

        import yaml
        from arbiter_engine.api import EngineSession, check
        model = {"domain": {"id": "d", "name": "n", "entity_types": ["S"],
                            "indicators": {"S": [{"name": "reading", "type": "NUMERIC",
                                                  "axioms": ["BOUNDEDNESS"],
                                                  "warning": 1, "critical": 2,
                                                  "window": "15m"}]}}}
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            yaml.safe_dump(model, handle)
        session = EngineSession()
        session.load_model(handle.name)
        session.add_entity("e", "S", properties={"reading": 5.0})
        envelope = check(session).to_dict()
        assert envelope["meta"]["schema_version"] == ENVELOPE_SCHEMA_VERSION

class TestTheDeclineVocabularyIsClassifiedAheadOfTheEngine:
    """Every reason the pinned engine RANGE can emit has a bucket here.

    The pin is `>=0.1.8,<0.2`, so a release inside it may add members to a closed
    enum -- the engine's own compatibility document says a patch may, and says a
    reader must treat the enum as three-valued. An unknown member lands in
    `unclassified_declines`, which is correct and is also a `--strict` failure on
    the day the pin moves, for cases this package has understood since 0.1.8.

    Classified BEFORE that release rather than after it. Measured on one model
    against both builds: a declared flow on a rail reading zero watts in declines
    `not_applicable` on 0.1.9 and `undefined_for_values` on the next build, with
    findings empty in both. Same fact, new name.
    """

    def _outcome(self, reason, **kw):
        from bmc_sensor_audit.detect.feeder import evaluate, FeedResult, Manifest
        envelope = {"findings": [], "not_checked": [
            {"entity_id": "r1", "entity_type": "Rail", "indicator": "pin_w",
             "axiom": "CONSERVATION", "reason": reason, "detail": "zero total"}]}
        return evaluate(envelope, {}, Manifest(domain_id="bmc"),
                        feed_result=FeedResult(), **kw)

    @pytest.mark.parametrize("reason", ["not_applicable", "undefined_for_values",
                                        "precondition_unmet"])
    def test_a_question_meaningless_on_these_values_does_not_fail_the_gate(self, reason):
        outcome = self._outcome(reason)
        assert outcome.inapplicable_declines, reason
        assert not outcome.unclassified_declines, reason
        assert outcome.exit_code == 0

    def test_a_model_defect_fails_because_this_package_wrote_the_model(self):
        """`missing_role` means a declaration the engine cannot run. The generator
        emitted it, so nothing else will notice it."""
        outcome = self._outcome("missing_role")
        assert outcome.core_case_declines
        assert outcome.exit_code == 1

    def test_an_unknown_member_still_overflows_by_name(self):
        """The control, and the one that matters most. Widening a closed set is
        only safe while the overflow still works -- otherwise the next member the
        engine adds is silently absorbed into whichever bucket sits nearest."""
        outcome = self._outcome("a_reason_no_build_emits")
        assert outcome.unclassified_declines
        assert not outcome.inapplicable_declines
        assert not outcome.core_case_declines

    def test_strict_still_fails_on_every_decline_bucket(self):
        """The buckets differ on the default exit and not under --strict. Pinned so
        widening the vocabulary cannot quietly soften the strict contract."""
        for reason in ("undefined_for_values", "precondition_unmet",
                       "a_reason_no_build_emits"):
            assert self._outcome(reason, strict_declines=True).exit_code == 1, reason
