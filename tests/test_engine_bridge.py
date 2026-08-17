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

import math

import pytest

yaml = pytest.importorskip("yaml", reason="canary needs PyYAML; installed with the engine")
engine_api = pytest.importorskip(
    "arbiter_engine.api",
    reason="arbiter-engine is the optional [detect] extra; Stage 1 does not require it")

from arbiter_engine.api import EngineSession, check, model_describe  # noqa: E402

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
