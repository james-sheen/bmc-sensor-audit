"""The four Stage 2 pillars, run against whatever `arbiter-engine` the pin admits.

This is the canary. `pyproject.toml` declares `arbiter-engine>=0.1.6,<0.2`, which is a
range, and upstream shipped five releases in forty-eight hours. Every behaviour Stage 2
will depend on is asserted here so a release inside that range cannot change one quietly.

**It also pins two payload LOCATIONS**, which is the part that has already bitten. The
describe payload carries two introspection keys at two different levels:

    unconsumed_observations   -> TOP LEVEL of the describe payload
    unread_fields             -> under the `model` block

Reading one location for the other returns `None`, and `None` reads as *this engine does
not support it* rather than *there is nothing to report*. A feeder that learns one
location and applies it to the other goes blind without an error. Both are asserted.

Skipped when the engine is absent, because Stage 1 is deliberately dependency-free and
the suite must run on a bring-up bench with nothing provisioned. That skip is a real gap
locally — CI installs the engine, which is where this is a gate rather than a courtesy.
"""

from __future__ import annotations

import importlib.metadata
import math
import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml", reason="canary needs PyYAML; installed with the engine")
engine_api = pytest.importorskip(
    "arbiter_engine.api",
    reason="arbiter-engine is the optional [detect] extra; Stage 1 does not require it")

from arbiter_engine.api import EngineSession, check, model_describe  # noqa: E402


def _require_the_pinned_engine() -> None:
    """Presence and correctness are different questions.

    The `importorskip` above answers whether an engine is installed and says nothing
    about WHICH one. Measured with a stale 0.1.0 present: this module produces six raw
    assertion errors about payload locations and stuck-at -- code-shaped failures for
    an environment-shaped cause, which cost a real diagnosis this week on an
    environment that violated the project's own rule about assessing the version the
    consumer pins.

    Absent stays a skip: Stage 1 does not require the engine and must run on a bench
    with nothing provisioned. Present-but-outside-the-pin fails deliberately, because
    it is an environment error and silence about it is what made the six failures
    expensive.

    The range is read from `pyproject.toml` rather than repeated here. A copy of the
    pin is a pin that can drift, which is the same class of cause as the parametrize
    list that let the exit-0 defect ship.
    """
    pyproject = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    pin = re.search(r'"arbiter-engine>=([0-9.]+),<([0-9.]+)"', pyproject.read_text())
    if pin is None:
        pytest.fail(f"could not find the arbiter-engine pin in {pyproject}. If the pin "
                    "was reworded, update this pattern -- this guard fails loudly "
                    "rather than silently checking nothing", pytrace=False)
    floor, ceiling = pin.groups()
    installed = importlib.metadata.version("arbiter-engine")

    def parts(version: str) -> tuple[int, ...]:
        return tuple(int(piece) for piece in version.split("."))

    try:
        outside = not parts(floor) <= parts(installed) < parts(ceiling)
    except ValueError:
        # A pre-release or local version. The pin scheme is plain X.Y.Z, so anything
        # else is outside it by construction rather than by comparison.
        outside = True
    if outside:
        pytest.fail(
            f"arbiter-engine {installed} is installed but this project pins "
            f">={floor},<{ceiling}. Environment error, not a code failure: "
            f"pip install --upgrade 'bmc-sensor-audit[detect]'", pytrace=False)


@pytest.fixture(scope="module", autouse=True)
def _the_engine_is_the_pinned_one():
    """Module-scoped, deliberately not module-LEVEL.

    A failure raised at import time is a collection error, and a collection error
    interrupts the whole session -- taking Stage 1 down with it, which needs no engine
    at all. As an autouse fixture the red is confined to this module and the rest of
    the suite still runs and still reports.
    """
    _require_the_pinned_engine()


WARN, CRIT = 10.0, 20.0
STUCK_AT_SAMPLES = 30          # floor measured at ~10; 30 is comfortably clear of it


@pytest.fixture(scope="module")
def envelope(tmp_path_factory):
    """One session exercising all four pillars, so the envelope is read once."""
    model = {"domain": {
        "id": "canary", "name": "Stage 2 canary",
        "entity_types": ["Sensor"],
        "indicators": {"Sensor": [{
            "name": "reading", "type": "NUMERIC",
            "axioms": ["BOUNDEDNESS", "STABILITY"],
            "warning": WARN, "critical": CRIT, "window": "15m",
            "expect_variation": True}]}}}
    path = tmp_path_factory.mktemp("canary") / "model.yaml"
    path.write_text(yaml.safe_dump(model))

    session = EngineSession()
    session.load_model(str(path))

    # 1. threshold breach
    session.add_entity("breach", "Sensor", properties={"reading": CRIT * 1.1})
    # 2. liveness: a frozen series with a live-looking current value
    session.add_entity("frozen", "Sensor", properties={"reading": 5.0})
    session.add_observations("frozen", "reading", [5.0] * STUCK_AT_SAMPLES,
                             interval_seconds=30)
    # 3. declared and absent
    session.add_entity("absent", "Sensor", properties={})
    # 4. healthy, plus an observation under a name the model never declares
    jitter = [5.0 + 0.5 * math.sin(i) for i in range(STUCK_AT_SAMPLES)]
    session.add_entity("healthy", "Sensor", properties={"reading": jitter[-1]})
    session.add_observations("healthy", "reading", jitter, interval_seconds=30)
    session.add_observations("healthy", "raeding", [1.0, 1.1, 1.2], interval_seconds=60)

    return {"check": check(session).to_dict(),
            "describe": model_describe(session).to_dict()}


def _findings(envelope, entity_id):
    return [f for f in (envelope["check"].get("findings") or [])
            if f.get("entity_id") == entity_id]


def _declines(envelope, entity_id):
    legs = (envelope["check"].get("not_checked")
            or envelope["check"].get("declines") or [])
    return [d for d in legs if d.get("entity_id") == entity_id]


class TestTheFourPillars:
    def test_pillar_1_threshold_breach_is_found(self, envelope):
        assert _findings(envelope, "breach"), "BOUNDEDNESS no longer reports a breach"

    def test_pillar_2_a_frozen_series_is_found(self, envelope):
        """The Stage 2 mission. A sensor still reporting a plausible value while its
        series has not moved is the case no threshold check can see."""
        assert _findings(envelope, "frozen"), \
            "STABILITY no longer reports a frozen series; stuck-at detection is the " \
            "whole point of Stage 2"

    def test_pillar_3_declared_and_absent_is_declined_not_ignored(self, envelope):
        """The tool's first sentence. A vacuous pass here is indistinguishable from a
        healthy board, which is the defect this engine was chosen to avoid."""
        declines = _declines(envelope, "absent")
        assert declines, "a declared sensor with no reading produced no decline"
        assert any(d.get("reason") == "missing_property" for d in declines), \
            f"expected a missing_property decline, got {[d.get('reason') for d in declines]}"

    def test_pillar_4_an_undeclared_name_is_surfaced(self, envelope):
        """Every Redfish-to-model mapping error rides on this. A typo that vanishes
        silently is a sensor the gate stops watching without saying so."""
        unconsumed = envelope["describe"].get("unconsumed_observations") or []
        assert any(u.get("property") == "raeding" for u in unconsumed), \
            "an observation under an undeclared name left no trace"

    def test_the_healthy_sensor_stays_clean(self, envelope):
        """The noise floor. Pillars that fire on everything prove nothing."""
        assert not _findings(envelope, "healthy"), \
            "a jittering in-range sensor produced a finding"


class TestThePayloadLocationsHaveNotMoved:
    """Two keys, two levels. Asserted separately from the pillars because a relocation
    is silent: the reader gets `None`, not an error."""

    def test_unconsumed_observations_is_top_level(self, envelope):
        describe = envelope["describe"]
        assert describe.get("unconsumed_observations") is not None, \
            "unconsumed_observations is no longer at the top level of describe"
        assert (describe.get("model") or {}).get("unconsumed_observations") is None, \
            "unconsumed_observations has appeared under `model` too; pick one and " \
            "update the feeder deliberately rather than reading whichever answers"

    def test_unread_fields_is_under_the_model_block(self, envelope):
        describe = envelope["describe"]
        assert "unread_fields" in (describe.get("model") or {}), \
            "unread_fields is no longer under the `model` block"
        assert describe.get("unread_fields") is None, \
            "unread_fields has moved to the top level; the feeder reads `model`"


class TestTheDenominatorIsDerivable:
    def test_invariants_equal_types_times_axioms(self, envelope):
        """AC5. The denominator is the product's promise: a clean result must be
        distinguishable from one where nothing was testable, and that is only true if
        the count of what was attempted can be checked independently."""
        checked = envelope["check"].get("checked") or {}
        assert checked.get("entities") == 4
        assert checked.get("invariants") == 4 * 2, \
            f"expected 4 entities x 2 axioms, got {checked}"


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    """Module-scoped, not class-scoped-as-a-method: pytest deprecates the latter and
    it silently stops sharing state, which would make these three tests rebuild the
    whole corpus model each time."""
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    from bmc_sensor_audit.detect.generator import generate
    from bmc_sensor_audit.inventory.entity_manager import load_declaration

    model, manifest = generate(
        load_declaration([str(root / "tests" / "fixtures" / "upstream")]))
    path = tmp_path_factory.mktemp("generated") / "model.yaml"
    path.write_text(yaml.safe_dump(model))
    session = EngineSession()
    session.load_model(str(path))
    return model, manifest, session


class TestTheGeneratedModelIsAcceptedWhole:
    """The generator's own oracle, and the reason it needs the engine present.

    A generated model can be well-formed as YAML and still be half-ignored: a key the
    engine does not read, an indicator nothing can reach. The engine reports both, so
    generation is checked against the consumer rather than against our idea of it.

    This is the plan's Phase 1 item 5, and it lives here rather than in
    `test_generator.py` because that file must run with no engine installed.
    """

    def test_the_engine_reads_every_key_the_generator_emits(self, generated):
        """`unread_fields` is the engine saying it ignored something. A generator
        emitting a key nothing reads produces a model that looks richer than it is."""
        _, _, session = generated
        unread = (model_describe(session).to_dict().get("model") or {}).get(
            "unread_fields") or []
        assert unread == [], f"the engine ignored generated keys: {unread}"

    def test_no_generated_declaration_is_unreachable(self, generated):
        """The mirror: an indicator declared but impossible to reach fires never, and
        counts toward the denominator while doing it."""
        _, _, session = generated
        unreachable = (model_describe(session).to_dict().get("model") or {}).get(
            "unreachable_declarations") or []
        assert unreachable == [], f"unreachable declarations: {unreachable}"

    def test_a_reading_below_its_lower_bound_is_found_and_translated(self, generated):
        """End to end, on real vendored thresholds: the negation transform fires, and
        the manifest turns the engine's inverted wording into what a person measured."""
        from bmc_sensor_audit.detect.generator import READING, READING_LOW
        _, manifest, session = generated
        sensor = next(s for s in manifest.sensors if s.lower[1] is not None)
        below = sensor.lower[1] - 0.1
        session.add_entity(sensor.entity_type, sensor.entity_type,
                           properties={READING: below, READING_LOW: -below})
        findings = [f for f in (check(session).to_dict().get("findings") or [])
                    if f.get("entity_id") == sensor.entity_type]
        assert findings, f"{sensor.declared_name} below {sensor.lower[1]} produced nothing"
        translated = manifest.translate_finding(findings[0])
        assert "BELOW" in translated and sensor.declared_name in translated, translated


class TestTheWholeStage2PathEndToEnd:
    """Declaration to exit code, through the real engine.

    Everything else in this file tests one seam. This runs the chain a user would:
    read the configs, generate the model, walk the machine, feed what is reading, and
    turn the envelope into a number CI can act on.
    """

    @staticmethod
    def _run(walks, *, strict=False):
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / "src"))
        from bmc_sensor_audit.detect.feeder import evaluate, feed
        from bmc_sensor_audit.detect.generator import generate
        from bmc_sensor_audit.inventory.diff import compare
        from bmc_sensor_audit.inventory.entity_manager import load_declaration
        from bmc_sensor_audit.inventory.redfish import walk_from_dict

        declaration = load_declaration([str(root / "tests" / "fixtures" / "upstream")])
        model, manifest = generate(declaration)
        import tempfile
        path = Path(tempfile.mkdtemp()) / "model.yaml"
        path.write_text(yaml.safe_dump(model))
        session = EngineSession()
        session.load_model(str(path))

        reports = []
        for entries in walks:
            walk = walk_from_dict({
                "format": "bmc-sensor-audit/walk/1", "chassis": ["/c/1"],
                "shapes_seen": ["sensors"], "errors": [],
                "sensors": [{"name": n, "path": f"/c/1/S/{i}", "reading": v,
                             "state": "Enabled", "health": "OK", "thresholds": {}}
                            for i, (n, v) in enumerate(entries)]})
            reports.append(compare(declaration, walk))

        result = feed(session, manifest, reports)
        outcome = evaluate(check(session).to_dict(), model_describe(session).to_dict(),
                           manifest, strict_declines=strict)
        return manifest, result, outcome

    def _pick(self, manifest):
        return next(s for s in manifest.sensors if s.has_lower and s.upper[0] is not None)

    def test_a_healthy_reading_passes_and_says_liveness_is_warming_up(self):
        """One walk is one sample. The gate passes, and the report can say so rather
        than implying it checked liveness from the first walk."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from bmc_sensor_audit.detect.generator import generate
        from bmc_sensor_audit.inventory.entity_manager import load_declaration
        _, manifest = generate(load_declaration(
            [str(Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "upstream")]))
        sensor = self._pick(manifest)
        midpoint = (sensor.upper[0] + sensor.lower[0]) / 2

        _, result, outcome = self._run([[(sensor.declared_name, midpoint)]])
        assert outcome.exit_code == 0, outcome.findings + outcome.core_case_declines
        assert result.warming_up, "liveness reported no warm-up on a single walk"
        assert outcome.data_declines, "the insufficient-sample decline was suppressed"

    def test_a_reading_below_its_floor_fails_with_translated_text(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from bmc_sensor_audit.detect.generator import generate
        from bmc_sensor_audit.inventory.entity_manager import load_declaration
        _, manifest = generate(load_declaration(
            [str(Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "upstream")]))
        sensor = self._pick(manifest)

        _, _, outcome = self._run([[(sensor.declared_name, sensor.lower[1] - 0.1)]])
        assert outcome.exit_code == 1
        assert any("BELOW" in f and sensor.declared_name in f for f in outcome.findings), \
            outcome.findings

    def test_a_frozen_series_is_caught_once_history_is_deep_enough(self):
        """The Stage 2 mission, through the whole chain: a sensor still reporting a
        plausible in-range value whose series has not moved."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from bmc_sensor_audit.detect.feeder import STUCK_AT_SAMPLE_FLOOR
        from bmc_sensor_audit.detect.generator import generate
        from bmc_sensor_audit.inventory.entity_manager import load_declaration
        _, manifest = generate(load_declaration(
            [str(Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "upstream")]))
        sensor = self._pick(manifest)
        stuck = (sensor.upper[0] + sensor.lower[0]) / 2

        walks = [[(sensor.declared_name, stuck)] for _ in range(STUCK_AT_SAMPLE_FLOOR * 3)]
        _, result, outcome = self._run(walks)
        assert not result.warming_up, "history did not accumulate past the floor"
        assert outcome.exit_code == 1, "a frozen sensor passed the gate"
        assert outcome.findings, "no finding for a series that never moved"


class TestTheVendoredCaptureRunsTheWholeStage2Path:
    """The same chain, on the one walk in this repository that this repository did
    not write.

    Everything above is synthetic. The four pillars build their own entities, and the
    end-to-end class builds walks from a dict literal -- self-consistent by
    construction, which is precisely the property that let a channel-reading defect
    survive 269 tests until real firmware found it. `walk_qemu_bletchley.json` is 28
    sensors from upstream `bmcweb` under QEMU, and until now it was read only by the
    shape layer, so the Stage 2 pipeline was still proven entirely against shapes we
    invented. Wiring it in here puts it under the daily canary.

    Both inputs are frozen files in this repository, so the counts are pinned
    deliberately: if one moves, the reader moved.
    """

    @classmethod
    @pytest.fixture(scope="class")
    def stage2(cls):
        import json
        import sys
        import tempfile
        from pathlib import Path
        root = pathlib.Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / "src"))
        from bmc_sensor_audit.detect.feeder import evaluate, feed
        from bmc_sensor_audit.detect.generator import generate
        from bmc_sensor_audit.inventory.diff import compare
        from bmc_sensor_audit.inventory.entity_manager import load_declaration
        from bmc_sensor_audit.inventory.redfish import walk_from_dict

        # The bletchley pair, not the whole corpus: this is the configuration that
        # machine actually booted. The coverage half of the same pairing is pinned in
        # test_vendored_corpus.py; this is the liveness half.
        declaration = load_declaration(
            [str(root / "tests" / "fixtures" / "upstream" / "meta" / "bletchley")])
        walk = walk_from_dict(json.loads(
            (root / "tests" / "fixtures" / "walk_qemu_bletchley.json").read_text()))

        model, manifest = generate(declaration)
        path = pathlib.Path(tempfile.mkdtemp()) / "model.yaml"
        path.write_text(yaml.safe_dump(model))
        session = EngineSession()
        session.load_model(str(path))

        report = compare(declaration, walk)
        result = feed(session, manifest, [report])
        outcome = evaluate(check(session).to_dict(),
                           model_describe(session).to_dict(), manifest)
        return report, result, outcome

    @staticmethod
    def _flagged(outcome) -> set[str]:
        """The declared names Stage 2 produced a finding for.

        Both translation branches put the sensor name first -- `NAME is above ...` and
        `NAME: ...` -- so the leading token is the name in either case.
        """
        return {finding.split()[0].rstrip(":") for finding in outcome.findings}

    def test_every_reading_the_capture_carries_reaches_the_model(self, stage2):
        """`skipped_not_modelled` is the number to watch. It counts declarations the
        generator chose not to model, and a channel the reader silently drops lands
        there -- which is the defect this capture found in the first place."""
        _, result, _ = stage2
        assert (result.fed, result.skipped_not_reading, result.skipped_not_modelled) \
            == (28, 0, 0)

    def test_the_engine_consumed_everything_it_was_given(self, stage2):
        """Two questions the engine will only answer if asked. A core-case decline is
        the engine saying a value Stage 1 called present never arrived; `unmapped` is
        it reporting observations the model never read. Both are mapping bugs, and
        both are silent."""
        _, _, outcome = stage2
        assert outcome.core_case_declines == []
        assert outcome.unmapped == []
        assert outcome.unclassified_declines == []

    def test_the_denominator_is_the_one_the_capture_supports(self, stage2):
        _, _, outcome = stage2
        assert outcome.checked == {"invariants": 80, "entities": 28}

    def test_one_walk_is_one_sample_and_liveness_says_so(self, stage2):
        """The exit code comes from bound breaches, not from liveness: every sensor is
        below the stuck-at floor on a single walk, and that is reported rather than
        suppressed."""
        _, result, outcome = stage2
        assert len(result.warming_up) == 28
        assert len(outcome.data_declines) == 28
        assert outcome.exit_code == 1

    def test_no_finding_contradicts_the_firmware_own_health_field(self, stage2):
        """Cross-validation against a producer that has never heard of this project.

        The capture carries `Status.Health` exactly as `bmcweb` computed it. Stage 2
        reaches its verdict independently, from entity-manager thresholds. A sensor
        this pipeline flags while the firmware calls it healthy would be a false
        positive against an independent oracle, which is the strongest negative
        evidence available without hardware.
        """
        report, _, outcome = stage2
        health = {m.declared.display_name: m.live.health for m in report.matches}
        flagged = self._flagged(outcome)
        contradicted = sorted(n for n in flagged if health.get(n) == "OK")
        assert contradicted == [], contradicted

    def test_where_the_firmware_disagrees_with_its_own_published_thresholds(self, stage2):
        """The other direction, which is not the mirror image.

        Two sensors are marked `Critical` by `bmcweb` while sitting inside the bounds
        `bmcweb` published for them in the same response. Stage 2 is silent on both,
        and silence is the right answer to those numbers -- so this is pinned as a
        known divergence in the firmware rather than as a miss in the reader.

        It is pinned by NAME and re-derived from the capture's own thresholds, so if
        the pipeline ever starts flagging these two, whoever sees the new finding
        reads this first. The cause would be a change in this reader; the capture
        cannot change.
        """
        report, _, outcome = stage2
        flagged = self._flagged(outcome)
        silent = sorted(m.declared.display_name for m in report.matches
                        if m.live.health not in (None, "OK")
                        and m.declared.display_name not in flagged)
        assert silent == ["P12V_FAN0", "P12V_FAN2"], silent

        for match in report.matches:
            if match.declared.display_name not in silent:
                continue
            lower = match.live.thresholds[("lower", "critical")]
            upper = match.live.thresholds[("upper", "warning")]
            assert lower < match.live.reading < upper, (
                f"{match.declared.display_name} reads {match.live.reading}, no longer "
                f"inside the {lower}..{upper} the capture publishes for it -- this pin "
                "describes a different situation than the one measured")
