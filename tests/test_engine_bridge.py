"""The four Stage 2 pillars, run against whatever `arbiter-engine` the pin admits.

This is the canary. `pyproject.toml` declares `arbiter-engine>=0.1.8,<0.2`, which is a
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
import json
import math
import pathlib
import re
import tempfile

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
        """End to end, on real vendored thresholds: the engine's native floor fires,
        and the manifest names the sensor on the board rather than the sanitised type.

        This is the test that has to keep passing across the retirement of the
        negation transform, because it asserts the OUTCOME -- a fan reading below its
        declared floor is reported as below it -- and never named the mechanism.
        """
        from bmc_sensor_audit.detect.generator import READING
        _, manifest, session = generated
        sensor = next(s for s in manifest.sensors if s.lower[1] is not None)
        below = sensor.lower[1] - 0.1
        session.add_entity(sensor.entity_type, sensor.entity_type,
                           properties={READING: below})
        findings = [f for f in (check(session).to_dict().get("findings") or [])
                    if f.get("entity_id") == sensor.entity_type]
        assert findings, f"{sensor.declared_name} below {sensor.lower[1]} produced nothing"
        assert findings[0]["problem_type"].startswith("below_"), (
            f"expected a floor-side finding, got {findings[0]['problem_type']}")
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
        """56 is 28 entities times two invariants -- BOUNDEDNESS and STABILITY on the
        one indicator each sensor now has.

        It was 80 while lower bounds rode on a mirrored `reading_low` indicator: a
        sensor declaring both a floor and a ceiling carried a second BOUNDEDNESS
        invariant that existed because the engine could not test downward, not
        because the board raised another question. Retiring the transform took 24
        invariants out of the denominator, and they were the transform's own
        overhead being counted as coverage.

        Derived rather than typed, so it stays true when the capture grows a sensor
        and false if an entity silently stops being modelled.
        """
        _, _, outcome = stage2
        assert outcome.checked == {"invariants": outcome.checked["entities"] * 2,
                                   "entities": 28}

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


class TestStuckAtAgainstRealFirmware:
    """Liveness detection against firmware, with ground truth somebody controlled.

    Every other stuck-at test in this repository builds its own series, so it can
    only show that the code does what the code was written to do. This one replays
    28 consecutive walks of an emulated machine in which ONE sensor was driven to a
    new value before each of the first 12 walks and then left alone. Same sensor,
    same firmware, same pipeline; the only variable is whether somebody kept
    changing it.

    The assertions do not hardcode which sensors should fire. The fixture carries
    the readings, so which sensors were genuinely constant is derivable from it,
    and the engine's verdict is checked against that derived set. A pinned list
    would only record what this build happened to do on the day.
    """

    FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" \
        / "stuck_at_qemu_bletchley.json"

    @classmethod
    def _fixture(cls):
        import json
        return json.loads(cls.FIXTURE.read_text())

    @staticmethod
    def _walks(data, count):
        """Rebuild whole walks from the skeleton and the per-sensor series.

        The 28 walks differ in exactly two fields, which is re-verified when the
        fixture is generated. Storing them whole would have added a quarter of a
        megabyte of near-identical JSON to carry 27 more copies of a tree shape
        `walk_qemu_bletchley.json` already proves.
        """
        import copy
        out = []
        for index in range(count):
            walk = copy.deepcopy(data["walk_skeleton"])
            for sensor in walk["sensors"]:
                series = data["series"][sensor["name"]]
                sensor["reading"] = series["reading"][index]
                sensor["health"] = series["health"][index]
            out.append(walk)
        return out

    @classmethod
    def _evaluate(cls, walks):
        import sys
        import tempfile
        root = pathlib.Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / "src"))
        from bmc_sensor_audit.detect.feeder import evaluate, feed
        from bmc_sensor_audit.detect.generator import generate
        from bmc_sensor_audit.inventory.diff import compare
        from bmc_sensor_audit.inventory.entity_manager import load_declaration
        from bmc_sensor_audit.inventory.redfish import walk_from_dict

        declaration = load_declaration(
            [str(root / "tests" / "fixtures" / "upstream" / "meta" / "bletchley")])
        model, manifest = generate(declaration)
        path = pathlib.Path(tempfile.mkdtemp()) / "model.yaml"
        path.write_text(yaml.safe_dump(model))
        session = EngineSession()
        session.load_model(str(path))
        reports = [compare(declaration, walk_from_dict(w)) for w in walks]
        result = feed(session, manifest, reports)
        outcome = evaluate(check(session).to_dict(),
                           model_describe(session).to_dict(), manifest)
        return result, outcome

    @staticmethod
    def _constant(walks):
        """Which sensors never moved, read straight out of the walks."""
        series = {}
        for walk in walks:
            for sensor in walk["sensors"]:
                series.setdefault(sensor["name"].replace(" ", "_"),
                                  []).append(sensor["reading"])
        return {name for name, values in series.items() if len(set(values)) == 1}

    @staticmethod
    def _stuck_at(outcome):
        return {f.split()[0].rstrip(":") for f in outcome.findings
                if "has not changed" in f}

    @classmethod
    def _driven(cls, data):
        return data["driven_sensor"].replace(" ", "_")

    def test_the_fixture_still_describes_the_experiment_it_claims(self):
        """The whole result rests on one sensor moving and then stopping. If an edit
        ever breaks that, every assertion below would still pass while testing
        something else entirely."""
        data = self._fixture()
        driven = data["driven_sensor"]
        phase_a = data["phase_a_walks"]
        readings = data["series"][driven]["reading"]
        assert len(readings) == data["walks"] == 28
        assert len(set(readings[:phase_a])) > 1, "the driven phase does not move"
        assert len(set(readings[phase_a:])) == 1, "the frozen phase is not frozen"
        assert "what this is not: a sensor failure" in data["_provenance"].lower(), \
            "the fixture no longer states that the freeze is an experiment"

    def test_while_it_was_driven_it_was_not_flagged(self):
        """And the silence is a verdict, not a shrug.

        `warming_up` is asserted empty for the driven sensor first: below the sample
        floor STABILITY declines rather than passing, and a decline would produce the
        same absence of a finding for an entirely different reason. Twelve walks is
        past the floor, so the engine looked and found nothing wrong.
        """
        data = self._fixture()
        walks = self._walks(data, data["phase_a_walks"])
        result, outcome = self._evaluate(walks)
        driven = self._driven(data)

        assert driven not in result.warming_up, \
            "below the sample floor: silence here would mean not-yet-checked"
        assert driven not in self._stuck_at(outcome)

    def test_the_engine_finds_exactly_the_sensors_that_did_not_move(self):
        """The claim that makes the rest evidence rather than anecdote.

        Which sensors sat still is a fact about the walks, computable without asking
        the engine. Over the driven phase the two sets are equal -- no false positive
        and nothing missed -- against readings production firmware served.
        """
        data = self._fixture()
        walks = self._walks(data, data["phase_a_walks"])
        _, outcome = self._evaluate(walks)
        assert self._stuck_at(outcome) == self._constant(walks)

    def test_once_it_stopped_being_driven_it_was_flagged(self):
        data = self._fixture()
        walks = self._walks(data, data["walks"])
        _, outcome = self._evaluate(walks)
        driven = self._driven(data)

        assert driven in self._stuck_at(outcome)
        finding = next(f for f in outcome.findings if f.startswith(driven))
        assert "has not changed" in finding
        assert "should vary" in finding

    def test_freezing_one_sensor_changed_exactly_one_verdict(self):
        """The experiment's whole value is that nothing else moved with it. A
        detector that reacted to the extra walks in general, rather than to this
        sensor stopping, would widen the set."""
        data = self._fixture()
        _, before = self._evaluate(self._walks(data, data["phase_a_walks"]))
        _, after = self._evaluate(self._walks(data, data["walks"]))
        assert self._stuck_at(after) - self._stuck_at(before) == {self._driven(data)}
        assert self._stuck_at(before) - self._stuck_at(after) == set()

    def test_the_verdict_is_about_the_window_not_the_lifetime(self):
        """The driven sensor is NOT constant across the 28 walks -- it moved for the
        first twelve -- yet it is flagged. That is correct and worth pinning: the
        detector answers whether a reading has stopped moving lately, so comparing
        it against a lifetime-constant set is the wrong oracle here, and the finding
        says so itself by naming the window it counted over.
        """
        data = self._fixture()
        walks = self._walks(data, data["walks"])
        _, outcome = self._evaluate(walks)
        driven = self._driven(data)

        assert driven not in self._constant(walks)
        assert driven in self._stuck_at(outcome)
        assert "in window" in next(f for f in outcome.findings
                                   if f.startswith(driven))


class TestRedundantSignalDisagreementEndToEnd:
    """The failure class nothing previously wired could see: a sensor that is
    present, reading, moving, and wrong.

    BOUNDEDNESS catches a reading outside its bounds and STABILITY catches one that
    has stopped moving. A drifted sensor is inside its bounds and still varying, so
    it passes both. The only thing that can catch it is a second reading of the same
    quantity, and the acceptance rule for this map says the class has to be injected
    and demonstrated rather than argued -- so this drives a declared pair apart and
    asserts the engine says so, in the operator's own names.
    """

    TOLERANCE = 0.10

    @pytest.fixture
    def paired(self, tmp_path):
        from bmc_sensor_audit.detect.generator import generate
        from bmc_sensor_audit.detect.supplemental import load_supplemental
        from bmc_sensor_audit.inventory.entity_manager import load_declaration
        path = tmp_path / "supplemental.json"
        path.write_text(json.dumps({
            "format": "bmc-sensor-audit/supplemental/1",
            "provenance": "test fixture; not a hardware claim",
            "redundant_groups": [{
                "sensors": ["MB_U73_THERM_LOCAL", "MB_U73_THERM_REMOTE"],
                "tolerance": self.TOLERANCE,
                "basis": "test fixture: both channels driven to one point, so the "
                         "pair is redundant BY CONSTRUCTION here. Not a claim that "
                         "a TMP421's die and remote diode agree on real hardware -- "
                         "they do not, which is why this file has to exist"}]}))
        upstream = pathlib.Path(__file__).resolve().parents[1] / "tests" / \
            "fixtures" / "upstream"
        declaration = load_declaration([str(upstream)])
        model, manifest = generate(declaration,
                                   supplemental=load_supplemental(path))
        return declaration, model, manifest

    def _run(self, paired, readings):
        from bmc_sensor_audit.detect.feeder import evaluate, feed
        from bmc_sensor_audit.inventory.diff import compare
        from bmc_sensor_audit.inventory.redfish import walk_from_dict
        declaration, model, manifest = paired
        walk = walk_from_dict({
            "format": "bmc-sensor-audit/walk/1",
            "chassis": ["/redfish/v1/Chassis/1"], "shapes_seen": ["sensors"],
            "errors": [],
            "sensors": [{"name": name, "reading": value, "state": "Enabled",
                         "health": "OK", "thresholds": {},
                         "path": f"/redfish/v1/Chassis/1/Sensors/s{i}"}
                        for i, (name, value) in enumerate(readings)]})
        report = compare(declaration, walk)
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        yaml.safe_dump(model, handle)
        handle.close()
        session = EngineSession()
        session.load_model(handle.name)
        result = feed(session, manifest, [report])
        outcome = evaluate(check(session).to_dict(),
                           model_describe(session).to_dict(), manifest,
                           feed_result=result)
        return result, outcome

    def _disagreements(self, outcome):
        return [f for f in outcome.findings if "redundant" in f]

    def test_a_pair_within_tolerance_is_clean(self, paired):
        """The control. Both channels inside the band and inside the tolerance."""
        _, outcome = self._run(paired, [("MB_U73_THERM_LOCAL", 30.0),
                                        ("MB_U73_THERM_REMOTE", 30.4)])
        assert self._disagreements(outcome) == []

    def test_a_drifted_channel_is_caught(self, paired):
        """The injection. 30.0 against 41.0 is 27% divergence on a 10% tolerance --
        and BOTH readings sit inside the declared 0..50 band and neither is frozen,
        so every other wired axiom passes them."""
        _, outcome = self._run(paired, [("MB_U73_THERM_LOCAL", 30.0),
                                        ("MB_U73_THERM_REMOTE", 41.0)])
        found = self._disagreements(outcome)
        assert len(found) == 1, outcome.findings
        assert outcome.exit_code == 1

    def test_the_finding_names_both_sensors_as_the_board_names_them(self, paired):
        """The engine names the sanitised entity type and the invented peer property
        key. A finding whose entire value is *these two disagree* has to name two
        things an operator can find on the hardware."""
        _, outcome = self._run(paired, [("MB_U73_THERM_LOCAL", 30.0),
                                        ("MB_U73_THERM_REMOTE", 41.0)])
        text = self._disagreements(outcome)[0]
        assert "MB_U73_THERM_LOCAL" in text
        assert "MB_U73_THERM_REMOTE" in text
        assert "peer_" not in text

    def test_the_other_axioms_really_do_pass_this_reading(self, paired):
        """Non-vacuity for the claim above, and the reason this axiom earns its
        place: if 41.0 broke a bound or read as frozen, the pair would be redundant
        evidence rather than the only evidence."""
        _, outcome = self._run(paired, [("MB_U73_THERM_LOCAL", 30.0),
                                        ("MB_U73_THERM_REMOTE", 41.0)])
        assert [f for f in outcome.findings if "redundant" not in f] == []

    def test_a_peer_that_stopped_reading_is_reported_not_silently_skipped(self, paired):
        """A pairing that quietly stops being checked looks exactly like a pairing
        that agrees."""
        result, outcome = self._run(paired, [("MB_U73_THERM_LOCAL", 30.0)])
        assert result.peers_not_reading == [
            "MB_U73_THERM_LOCAL -> MB_U73_THERM_REMOTE"]
        assert self._disagreements(outcome) == []

    def test_an_absent_peer_does_not_fail_the_gate_as_a_mapping_bug(self, paired):
        """Stage 1 already reports that sensor as absent, precisely. Counting the
        engine's CONSISTENCY `missing_property` as a core-case decline failed the
        gate twice for one fact and asserted the name mapping was wrong."""
        _, outcome = self._run(paired, [("MB_U73_THERM_LOCAL", 30.0)])
        assert outcome.core_case_declines == []
        assert any("CONSISTENCY" in d for d in outcome.data_declines)


#: The declared efficiency floor for the fixture below. A module constant because
#: the fixture is now shared: two later classes exercise the same generated model
#: against different readings, and a fixture reachable from one class only would
#: have been copied instead.
LOSS_MARGIN = 0.15


@pytest.fixture
def flowed(tmp_path):
    """The vendored Mt.Jade declaration, generated with a declared PSU flow."""
    from bmc_sensor_audit.detect.generator import generate
    from bmc_sensor_audit.detect.supplemental import load_supplemental
    from bmc_sensor_audit.inventory.entity_manager import load_declaration
    path = tmp_path / "supplemental.json"
    path.write_text(json.dumps({
        "format": "bmc-sensor-audit/supplemental/1",
        "provenance": "test fixture; the margin is not a datasheet figure",
        "flows": [{"input": "PSU0_PINPUT", "outputs": ["PSU0_POUTPUT"],
                   "loss_margin": LOSS_MARGIN,
                   "basis": "test fixture: a stand-in for the efficiency floor "
                            "a real deployment would take off the PSU "
                            "datasheet. The number is the operator's to supply "
                            "and this one establishes nothing about Mt.Jade"}]}))
    upstream = pathlib.Path(__file__).resolve().parents[1] / "tests" / \
        "fixtures" / "upstream"
    declaration = load_declaration([str(upstream)])
    model, manifest = generate(declaration, supplemental=load_supplemental(path))
    return declaration, model, manifest


class TestConservationEndToEnd:
    """PSU efficiency collapse, on the real vendored Mt.Jade declaration.

    The map that asked for this recorded a material gap -- *the vendored configs
    contain no PSU power-pair declaration* -- and the gap was really two facts. The
    Delta PSU already vendored DOES declare `pin` and `pout1`, but its names are
    templated, so nothing can ever match them; and the reader was not constructing
    rails declared by `Labels` at all. `ampere/mtjade.json` was vendored at the
    pin for a pair that is neither templated nor invisible.
    """

    MARGIN = LOSS_MARGIN

    def _run(self, flowed, pin_watts, pout_watts, samples=12, drift=0.01):
        from bmc_sensor_audit.detect.feeder import evaluate, feed
        from bmc_sensor_audit.inventory.diff import compare
        from bmc_sensor_audit.inventory.redfish import walk_from_dict
        declaration, model, manifest = flowed
        reports = []
        for step in range(samples):
            walk = walk_from_dict({
                "format": "bmc-sensor-audit/walk/1",
                "chassis": ["/redfish/v1/Chassis/1"], "shapes_seen": ["sensors"],
                "errors": [],
                "sensors": [{"name": name, "reading": value + step * drift,
                             "state": "Enabled", "health": "OK", "thresholds": {},
                             "path": f"/redfish/v1/Chassis/1/Sensors/s{i}"}
                            for i, (name, value) in enumerate(
                                [("PSU0_PINPUT", pin_watts),
                                 ("PSU0_POUTPUT", pout_watts)])]})
            reports.append(compare(declaration, walk))
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        yaml.safe_dump(model, handle)
        handle.close()
        session = EngineSession()
        session.load_model(handle.name)
        result = feed(session, manifest, reports)
        outcome = evaluate(check(session).to_dict(),
                           model_describe(session).to_dict(), manifest,
                           feed_result=result)
        return result, outcome

    def _imbalances(self, outcome):
        return [f for f in outcome.findings if "imbalance" in f]

    def test_a_psu_inside_its_loss_margin_is_clean(self, flowed):
        """800 W in, 720 W out: a 10% loss against a 15% margin."""
        _, outcome = self._run(flowed, 800.0, 720.0)
        assert self._imbalances(outcome) == []

    def test_an_efficiency_collapse_is_caught(self, flowed):
        """800 W in, 500 W out. Neither reading is out of bounds -- neither has any
        bound at all -- and neither is frozen, so this is invisible to everything
        else this tool wires."""
        _, outcome = self._run(flowed, 800.0, 500.0)
        found = self._imbalances(outcome)
        assert len(found) == 1, outcome.findings
        assert "PSU0_PINPUT" in found[0]
        assert outcome.exit_code == 1

    def test_a_flow_reading_with_no_bounds_is_still_modelled(self, flowed):
        """The ordinary rule excludes a sensor with nothing to bound against. Both
        Mt.Jade power rails declare no threshold, so that rule would have dropped
        exactly the readings the check needs and left it silently never running."""
        _, model, manifest = flowed
        assert manifest.type_for("PSU0_PINPUT") is not None
        assert manifest.type_for("PSU0_POUTPUT") is not None

    def test_boundedness_is_not_asked_of_an_unbounded_flow_reading(self, flowed):
        """A modelled sensor with no threshold on either side would decline
        `no_threshold` every pass -- a decline about the model, not the board."""
        _, model, manifest = flowed
        indicator = model["domain"]["indicators"][
            manifest.type_for("PSU0_PINPUT")][0]
        assert "BOUNDEDNESS" not in indicator["axioms"]
        assert "CONSERVATION" in indicator["axioms"]
        assert "STABILITY" in indicator["axioms"], (
            "a flow reading that has frozen is still a dead sensor")

    def test_the_peer_series_is_not_reported_as_an_unread_feed(self, flowed):
        """CONSERVATION reads a series for each output, and the engine's
        unconsumed-observation report is built from declared INDICATORS -- so the
        peer arrives there as `undeclared_property`. `unmapped` fails the gate, so
        a healthy board with a declared flow would have exited 1 every run."""
        _, outcome = self._run(flowed, 800.0, 720.0)
        assert outcome.unmapped == []
        assert outcome.exit_code == 0

    def test_a_genuinely_unread_property_is_still_reported(self, flowed):
        """Non-vacuity for the filter above: it excludes peers this model declares,
        by (entity, property) pair, and nothing else."""
        from bmc_sensor_audit.detect.feeder import evaluate
        _, _, manifest = flowed
        entity = manifest.type_for("PSU0_PINPUT")
        describe = {"unconsumed_observations": [
            {"entity_id": entity, "property": "peer_SOMETHING_ELSE",
             "observations": 4, "reason": "undeclared_property"}]}
        outcome = evaluate({"meta": {"schema_version": 1}}, describe, manifest)
        assert len(outcome.unmapped) == 1
        assert "peer_SOMETHING_ELSE" in outcome.unmapped[0]


class TestTheZeroInputDeclineArrivedInsideThePin:
    """0.1.8 gave CONSERVATION a decline where 0.1.6 and 0.1.7 said nothing.

    **This is the canary doing its job, and it took a measurement to find.** The pin
    is a RANGE. A declared flow whose total input is at or below zero produced an
    empty problem list on the engine this module was written against, and declines
    `not_applicable` now -- real on any idle or powered-off rail, and it arrived as
    a reason the feeder had no member for, printing *declines this build does not
    recognise*.

    Nothing broke: an unclassified decline is reported prominently and only fails
    the gate under `--strict-declines`, which is the whole point of the third
    bucket. The LABEL was wrong, and a label that is wrong about the engine is
    exactly what this file exists to catch before an operator meets it.

    **The shape had to be measured too.** The obvious test -- feed zero -- passed
    the first time for the wrong reason: this class's runner drifts each sample by
    0.01 so STABILITY has something to see, which lifts the window total above zero.
    A flat rail is what reaches the decline, and a flat rail is also frozen, so the
    two checks arrive together and are asserted apart.
    """

    def test_a_rail_flat_at_zero_declines_conservation(self, flowed):
        _, outcome = TestConservationEndToEnd()._run(flowed, 0.0, 0.0, drift=0.0)
        assert outcome.inapplicable_declines, (
            "a declared flow flat at zero produced no decline; on 0.1.8 CONSERVATION "
            "declines not_applicable when the total input is at or below zero")
        assert not outcome.unclassified_declines, (
            "the reason is measured and named; filing it under unrecognised prints a "
            "sentence about this build that stopped being true")

    def test_the_frozen_finding_is_separate_from_the_decline(self, flowed):
        """A rail flat at zero is both unbalanceable and dead, and the report has to
        carry both. A decline that swallowed the run would lose the finding that
        actually needs an engineer."""
        _, outcome = TestConservationEndToEnd()._run(flowed, 0.0, 0.0, drift=0.0)
        # Matched on the manifest's translation, the way the sibling imbalance
        # assertions in this file are: `findings` carries the operator's sentence,
        # not the engine's `problem_type`.
        assert [f for f in outcome.findings if "has not changed" in f], outcome.findings
        assert outcome.exit_code == 1, "the frozen rail is what fails this gate"

    def test_the_decline_alone_does_not_fail_the_gate(self, flowed):
        """Isolated on a moving series whose total stays negative, so nothing else
        fires. A firmware gate that went red because a rail was idle would be
        switched off within a week, taking the signal with it."""
        _, outcome = TestConservationEndToEnd()._run(flowed, -5.0, 1.0)
        assert outcome.inapplicable_declines
        assert outcome.findings == []
        assert outcome.exit_code == 0

    def test_strict_declines_still_reaches_it(self):
        """The flag means *tell me about everything that could not be judged*, and a
        bucket it could not reach would be a hole in the one thing it promises."""
        from bmc_sensor_audit.detect.feeder import DetectOutcome

        outcome = DetectOutcome(strict=True)
        outcome.inapplicable_declines.append("PSU0 [CONSERVATION] not_applicable")
        assert outcome.exit_code == 1

    def test_a_positive_flow_is_still_judged(self, flowed):
        """Non-vacuity: the decline is about the DATA, and real data still gets a
        verdict. A build that declined everything would pass the tests above."""
        _, outcome = TestConservationEndToEnd()._run(flowed, 800.0, 500.0)
        assert outcome.findings
        assert not outcome.inapplicable_declines


class TestTheGeneratedModelNeverAsksAnUndeclaredConservation:
    """0.1.8 also made CONSERVATION decline `no_balance_declared` when an indicator
    lists the axiom without declaring what balances against what.

    Guarded here as a property of the model this tool GENERATES rather than as a
    fourth decline bucket. The generator adds the axiom and the block in one place,
    so the decline can only arrive if that pairing comes apart -- and catching it at
    generation is catching it before a run rather than after one.
    """

    def test_every_conservation_indicator_declares_its_balance(self, flowed):
        _, model, _ = flowed
        asked = [(entity, indicator)
                 for entity, indicators in model["domain"]["indicators"].items()
                 for indicator in indicators
                 if "CONSERVATION" in indicator.get("axioms", [])]
        assert asked, "no indicator asks CONSERVATION; this proves nothing"
        for entity, indicator in asked:
            block = indicator.get("conservation")
            assert isinstance(block, dict), f"{entity} asks CONSERVATION with no block"
            assert block.get("input_property"), f"{entity}: no input_property"
            assert block.get("output_properties"), f"{entity}: no output_properties"


class TestTheAttestationArtifact:
    """A per-run record that survives the run, and admits what it does not cover.

    The map listed `attest` as uncharacterized and said *probe first, wire second*.
    Two facts came out of probing it, and both shape this artifact: it requires
    `check()` to have run first and refuses honestly otherwise, and it is the only
    surface that carries per-finding EVIDENCE -- `check()` renders a finding as five
    keys and drops the measurements entirely.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def artifact(tmp_path_factory):
        import subprocess
        import sys
        root = pathlib.Path(__file__).resolve().parents[1]
        out = tmp_path_factory.mktemp("attest") / "attestation.json"
        result = subprocess.run(
            [sys.executable, "-m", "bmc_sensor_audit.cli", "detect",
             "--config", str(root / "tests/fixtures/upstream/meta/bletchley"),
             "--walk", str(root / "tests/fixtures/walk_qemu_bletchley.json"),
             "--attest-out", str(out)],
            capture_output=True, text=True,
            env={"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin",
                 "HOME": "/root"})
        assert out.is_file(), result.stdout + result.stderr
        return json.loads(out.read_text())

    def test_it_records_what_was_checked(self, artifact):
        assert artifact["checked"]["entities"] == 28
        assert artifact["findings"]

    def test_it_records_what_was_DECLINED(self, artifact):
        """The half a compliance reader needs. An axiom that could not be evaluated
        is not one that passed, and an artifact listing only findings reads as a
        clean bill of health for every question nobody managed to ask."""
        assert artifact["not_checked"], "the artifact records no declines at all"
        assert all(d["reason"] for d in artifact["not_checked"])
        assert any(d["axiom"] == "STABILITY" for d in artifact["not_checked"])

    def test_every_finding_carries_the_numbers_behind_it(self, artifact):
        """The reason `attest` is used at all. `check()` says *reading exceeds
        critical threshold* and never says 14.8985 against 12.61."""
        assert len(artifact["evidence"]) == len(artifact["findings"])
        for entry in artifact["evidence"]:
            measurement = entry["measurement"]
            assert isinstance(measurement.get("value"), (int, float))
            assert isinstance(measurement.get("threshold"), (int, float))
            assert measurement.get("bound") in ("upper", "lower")

    def test_it_carries_the_engines_own_boundary_verbatim(self, artifact):
        """The engine declining to be called an attestation service. Quoted rather
        than paraphrased: rewording it would make this repository the author of a
        disclaimer the engine wrote."""
        boundary = artifact["engine"]["boundary"]
        assert boundary
        assert "production attestation records are v0.2" in boundary

    def test_it_names_sensors_as_the_board_names_them(self, artifact):
        """An artifact naming a sanitised entity type is one nobody can act on six
        months later."""
        assert any(f["sensor"] == "P12V_AUX" for f in artifact["findings"])

    def test_it_pins_the_envelope_schema_version(self, artifact):
        assert artifact["engine"]["schema_version"] == 1

    def test_a_problem_type_the_engine_will_not_attest_is_recorded(self, tmp_path):
        """Not dropped. An artifact that silently omits what it could not attest
        claims a completeness it does not have."""
        from bmc_sensor_audit.detect.attestation import build_attestation

        class _Refusing:
            def to_dict(self):
                return {"meta": {"source": "unavailable", "reason": "nope"}}

        class _Manifest:
            sensors = ()

            def translate_finding(self, finding):
                return "x"

        envelope = {"findings": [{"entity_id": "e", "problem_type": "p:reading"}],
                    "meta": {"schema_version": 1}}
        built = build_attestation(None, envelope, {}, _Manifest(), target="t",
                                 attest_fn=lambda *a, **k: _Refusing())
        assert built["unattested"] == ["p:reading: nope"]
        assert built["evidence"] == []


class TestTheWholeCorpusFinishesInATimeAGateCanLiveWith:
    """The scale question, asked of the corpus this tool actually meets.

    Full measurement in `docs/stage2/s3-corpus-scale.md`, which is where the
    numbers live -- restating them here is how the two copies drift. Two things it
    found that change what is worth asserting: getting the model in, not `check()`,
    dominates at this size, inverting the earlier prediction; and the population the
    engine is asked about is the MODELLED set, not the declared one. That first
    finding has since been re-attributed -- the cost is PyYAML, inside the load call
    rather than beside it, not the engine's own loader -- and the doc carries the
    dated note with figures measured here.

    The ceiling is deliberately about thirty times the measured median. Measured
    spread is roughly a third of the median, so a tight bound would go red on
    ordinary jitter -- and a row that fails for a legitimate reason every few runs
    is one people learn to skip, which costs more than the row is worth. This
    catches an order-of-magnitude regression from a release inside the pin, which
    is the failure it can actually see.
    """

    CEILING_SECONDS = 10.0

    @staticmethod
    @pytest.fixture(scope="class")
    def run_over_the_corpus(tmp_path_factory):
        import time

        from bmc_sensor_audit.detect.generator import generate
        from bmc_sensor_audit.inventory.entity_manager import load_declaration
        upstream = pathlib.Path(__file__).resolve().parents[1] / "tests" / \
            "fixtures" / "upstream"
        declaration = load_declaration([str(upstream)])
        model, manifest = generate(declaration)
        path = tmp_path_factory.mktemp("scale") / "model.yaml"
        path.write_text(yaml.safe_dump(model))

        started = time.perf_counter()
        session = EngineSession()
        session.load_model(str(path))
        for sensor in manifest.sensors:
            session.add_entity(sensor.entity_type, sensor.entity_type,
                               properties={"reading": 25.0})
            session.add_observations(sensor.entity_type, "reading",
                                     [25.0 + 0.1 * i for i in range(12)],
                                     interval_seconds=60.0)
        envelope = check(session).to_dict()
        return time.perf_counter() - started, declaration, manifest, envelope

    def test_the_whole_path_finishes_under_the_ceiling(self, run_over_the_corpus):
        elapsed, _, _, _ = run_over_the_corpus
        assert elapsed < self.CEILING_SECONDS, (
            f"a full-corpus detect took {elapsed:.1f}s against a {self.CEILING_SECONDS}s "
            f"ceiling. That is an order of magnitude off the measurement in "
            f"docs/stage2/s3-corpus-scale.md, so re-measure before raising this")

    def test_the_denominator_is_the_modelled_population_not_the_declared_one(
            self, run_over_the_corpus):
        """The number a scale claim has to name. `377 entities` overstates what the
        engine is asked to do by more than a factor of two, and `check()` cost is
        driven by what was fed."""
        _, declaration, manifest, envelope = run_over_the_corpus
        assert envelope["checked"]["entities"] == len(manifest.sensors)
        assert len(manifest.sensors) < len(declaration.sensors), (
            "every declaration is now modelled; the exclusion ledger has stopped "
            "excluding, which is a bigger change than a timing one")
        assert envelope["checked"]["invariants"] == 2 * len(manifest.sensors), (
            "two axioms per modelled sensor is the shape the measurement assumed")

    def test_nothing_was_dropped_between_the_declaration_and_the_ledger(
            self, run_over_the_corpus):
        """Non-vacuity for the assertion above: `modelled < declared` is only
        honest if the difference is accounted for rather than lost."""
        _, declaration, manifest, _ = run_over_the_corpus
        excluded = sum(len(names) for names in manifest.excluded.values())
        assert len(manifest.sensors) + excluded == len(declaration.sensors), (
            f"{len(declaration.sensors)} declared, {len(manifest.sensors)} modelled, "
            f"{excluded} excluded -- these do not add up, so something was dropped "
            f"without a reason being recorded")
