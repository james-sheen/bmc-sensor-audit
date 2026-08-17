"""Golden tests for the Stage 2 generator, over the pinned vendored corpus.

No engine required: the generator emits plain data, so the model's shape can be pinned
without installing the optional extra. The engine-side self-check — load the generated
model and ask it what it did not read — lives in `test_engine_bridge.py`, which skips
when the extra is absent.

**The load-bearing test here is `test_every_declaration_is_either_generated_or_excluded`.**
A generator that silently drops what it cannot express produces a model that looks
complete and audits less than the reader believes. Counting is the only thing that
catches that, and it is the same discipline as the coverage report's own denominator.
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
    READING, READING_LOW, generate)
from bmc_sensor_audit.inventory.entity_manager import (  # noqa: E402
    ANY_TEMPLATE, load_declaration)


@pytest.fixture(scope="module")
def built():
    declaration = load_declaration([str(UPSTREAM)])
    model, manifest = generate(declaration)
    return declaration, model, manifest


class TestNothingVanishes:
    def test_every_declaration_is_either_generated_or_excluded(self, built):
        """The completeness pin. Every declared sensor must be accounted for on
        exactly one side of the ledger — no silent drops, no double counting."""
        declaration, _, manifest = built
        excluded = sum(len(names) for names in manifest.excluded.values())
        assert len(manifest.sensors) + excluded == len(declaration.sensors), (
            f"{len(declaration.sensors)} declared, "
            f"{len(manifest.sensors)} generated + {excluded} excluded")

    def test_every_exclusion_states_a_reason(self, built):
        _, _, manifest = built
        assert manifest.excluded, "nothing was excluded, which cannot be right"
        for reason, names in manifest.excluded.items():
            assert reason and names, f"empty exclusion bucket: {reason!r}"

    def test_the_golden_counts(self, built):
        """Pinned against the vendored corpus, which is itself pinned to
        `openbmc/entity-manager@0ada0483`. A change here means the generator's
        behaviour moved and should be explained, not re-baselined by reflex."""
        _, _, manifest = built
        counts = manifest.counts()
        assert counts["generated"] == 65
        assert counts["excluded_templated_name"] == 11
        assert counts["excluded_not_a_sensor"] == 14
        assert counts["excluded_no_thresholds"] == 4
        assert counts["with_lower_bound"] == 53


class TestTheModelIsWellFormed:
    def test_one_entity_type_per_generated_sensor(self, built):
        _, model, manifest = built
        types = model["domain"]["entity_types"]
        assert len(types) == len(manifest.sensors)
        assert len(set(types)) == len(types), "entity type names collided"

    def test_every_type_has_at_least_one_indicator(self, built):
        _, model, _ = built
        for entity_type in model["domain"]["entity_types"]:
            assert model["domain"]["indicators"].get(entity_type), entity_type

    def test_entity_type_names_are_identifiers(self, built):
        _, model, _ = built
        for entity_type in model["domain"]["entity_types"]:
            assert entity_type.replace("_", "").isalnum(), entity_type
            assert not entity_type[0].isdigit(), f"{entity_type} starts with a digit"

    def test_the_manifest_maps_every_type_back_to_a_sensor_name(self, built):
        """The sanitisation is lossy and every finding names the sanitised form. A
        reader who cannot get back to the sensor on the board has a finding they
        cannot act on."""
        _, model, manifest = built
        for entity_type in model["domain"]["entity_types"]:
            assert manifest.describe_indicator(entity_type, READING) != \
                f"{entity_type}.{READING}", f"no mapping back for {entity_type}"

    def test_the_model_serialises(self, built):
        _, model, _ = built
        assert json.loads(json.dumps(model))["domain"]["id"]


class TestTemplatedNamesNeverReachTheEngine:
    def test_no_generated_type_carries_a_template_variable(self, built):
        """G3. A `$bus` name becomes an entity type nothing can ever match, and the
        engine has no way to report that it will never fire."""
        _, model, _ = built
        for entity_type in model["domain"]["entity_types"]:
            assert not ANY_TEMPLATE.search(entity_type), entity_type

    def test_the_templated_ones_are_named_in_the_manifest(self, built):
        declaration, _, manifest = built
        excluded = manifest.excluded.get("templated_name", [])
        declared_templated = [s for s in declaration.sensors
                              if ANY_TEMPLATE.search(s.name)]
        assert len(excluded) == len(declared_templated)


class TestFourBoundFidelity:
    def test_lower_bounds_survive_as_a_negated_indicator(self, built):
        """AC4. BOUNDEDNESS is upper-only, so a lower bound rides on the negated
        reading. Without this a stopped fan reads clean."""
        _, model, manifest = built
        with_lower = [s for s in manifest.sensors if s.has_lower]
        assert with_lower
        for sensor in with_lower:
            names = [i["name"] for i in model["domain"]["indicators"][sensor.entity_type]]
            assert READING_LOW in names, f"{sensor.declared_name} lost its lower bound"

    def test_the_negation_preserves_threshold_ordering(self, built):
        """Critical must be the more extreme number after negation, or the engine
        reads the pair backwards and warns where it should alarm."""
        _, model, manifest = built
        for sensor in (s for s in manifest.sensors if s.has_lower):
            low = next(i for i in model["domain"]["indicators"][sensor.entity_type]
                       if i["name"] == READING_LOW)
            assert low["critical"] >= low["warning"], (
                f"{sensor.declared_name}: negated critical {low['critical']} is not "
                f"beyond warning {low['warning']}")

    def test_a_negated_bound_is_the_arithmetic_negation(self, built):
        _, model, manifest = built
        sensor = next(s for s in manifest.sensors
                      if s.lower[1] is not None and s.lower[0] is not None)
        low = next(i for i in model["domain"]["indicators"][sensor.entity_type]
                   if i["name"] == READING_LOW)
        assert low["warning"] == -sensor.lower[0]
        assert low["critical"] == -sensor.lower[1]

    def test_levels_beyond_warning_and_critical_are_recorded_not_folded(self, built):
        """entity-manager also declares `hard_shutdown` and `non_recoverable`; the
        engine has two slots. Folding one into `critical` would move the alarm point
        to a different number and call it the same thing."""
        _, _, manifest = built
        unmapped = [u for s in manifest.sensors for u in s.unmapped_levels]
        assert unmapped, "the corpus carries extra levels; none were recorded"
        for bound, level, value in unmapped:
            assert level not in ("warning", "critical")
            assert isinstance(value, float)


class TestTranslationBackToTheSensor:
    def test_a_lower_bound_finding_is_not_reported_as_exceeding(self, built):
        """The engine's text is right about the model and wrong about the world: a
        stopped fan produces `reading_low exceeds critical threshold`. Reported raw,
        that says a stopped fan is spinning too fast."""
        _, _, manifest = built
        sensor = next(s for s in manifest.sensors if s.lower[1] is not None)
        finding = {"entity_id": sensor.entity_type, "severity": "critical",
                   "problem_type": f"threshold_exceeded:{READING_LOW}",
                   "reason": f"{READING_LOW} exceeds critical threshold"}
        translated = manifest.translate_finding(finding)
        assert "BELOW" in translated
        assert sensor.declared_name in translated
        assert READING_LOW not in translated

    def test_an_upper_bound_finding_still_reads_as_above(self, built):
        _, _, manifest = built
        sensor = next(s for s in manifest.sensors if s.upper[1] is not None)
        finding = {"entity_id": sensor.entity_type, "severity": "critical",
                   "problem_type": f"threshold_exceeded:{READING}",
                   "reason": f"{READING} exceeds critical threshold"}
        assert "above" in manifest.translate_finding(finding)

    def test_an_unrecognised_shape_falls_back_to_the_engines_own_text(self, built):
        """The `problem_type` shape was measured, not specified. If it moves, this
        must degrade to the engine's wording rather than invent one."""
        _, _, manifest = built
        finding = {"entity_id": "nothing", "severity": "critical",
                   "problem_type": "some_new_shape", "reason": "engine wording"}
        assert manifest.translate_finding(finding) == "engine wording"


class TestDeclarationDefectsTravel:
    def test_the_two_upstream_polarity_defects_reach_the_manifest(self, built):
        """A contradiction in the expectation source is invisible to anything that
        only watches readings, so it must not be normalised away by generation."""
        _, _, manifest = built
        conflicts = [a for a in manifest.anomalies
                     if "threshold_direction_conflict" in a]
        assert len(conflicts) == 2, manifest.anomalies


class TestExpectVariationIsAChoice:
    def test_it_is_on_by_default_because_liveness_is_the_mission(self, built):
        _, model, _ = built
        stability = [i for inds in model["domain"]["indicators"].values()
                     for i in inds if "STABILITY" in i["axioms"]]
        assert stability
        assert all(i.get("expect_variation") for i in stability)

    def test_it_can_be_turned_off(self):
        """It is the setting most likely to false-positive on real hardware — a rail
        that genuinely reports an identical value every walk is flat without being
        broken — and that calibration cannot be done without a real capture."""
        declaration = load_declaration([str(UPSTREAM)])
        model, manifest = generate(declaration, expect_variation=False)
        indicators = [i for inds in model["domain"]["indicators"].values() for i in inds]
        assert not any(i.get("expect_variation") for i in indicators)
        assert manifest.expect_variation is False


class TestGeneratedOutputIsSubjectToTheHygieneRules:
    """A generated model is a new artifact, and artifacts are where hardware identity
    escapes. Sensor names travel from the declaration into entity-type names and into
    the manifest, so the same rules that guard the repository guard the output.

    This is a test rather than a runtime call because `tools/` is not part of the
    package — Stage 1 ships no scanner. It still catches the real case: a declaration
    carrying a literal serial, part number or asset tag in a sensor name would put it
    straight into a model somebody commits.
    """

    def test_the_model_generated_from_the_corpus_is_clean(self, built, tmp_path):
        import yaml
        sys.path.insert(0, str(ROOT / "tools"))
        import hygiene_check

        _, model, manifest = built
        artifact = tmp_path / "generated.yaml"
        artifact.write_text(yaml.safe_dump(model))
        (tmp_path / "manifest.json").write_text(json.dumps(manifest.to_dict()))

        paths = [p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()]
        hits = hygiene_check.scan(paths, tmp_path, rules=hygiene_check.RULES)
        assert hits == [], "\n".join(f"{h[0]}:{h[1]} [{h[2].name}]" for h in hits)

    def test_the_scan_would_catch_an_identity_bearing_name(self, tmp_path):
        """The paired positive. Without it the test above passes because the corpus
        happens to be clean, not because anything is being checked."""
        import yaml
        sys.path.insert(0, str(ROOT / "tools"))
        import hygiene_check

        artifact = tmp_path / "generated.yaml"
        artifact.write_text(yaml.safe_dump(
            {"domain": {"id": "x",
                        "note": '{"SerialNumber": "CN7082019L003A"}'}}))  # hygiene: synthetic
        hits = hygiene_check.scan([Path("generated.yaml")], tmp_path,
                                  rules=hygiene_check.RULES)
        assert [h[2].name for h in hits] == ["redfish_inventory_field"]
