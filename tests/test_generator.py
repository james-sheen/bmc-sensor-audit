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
import pathlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "tests" / "fixtures" / "upstream"
sys.path.insert(0, str(ROOT / "src"))

from bmc_sensor_audit.detect.generator import (  # noqa: E402
    BOUND_OF_PROBLEM, READING, generate)
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
        behaviour moved and should be explained, not re-baselined by reflex.

        Moved twice, and both moves have an explanation rather than a
        re-baseline.

        65 -> 68: the declaration reader began counting the further channels of a
        multi-channel part -- `Name1` and up -- which it had been discarding.
        Three are in `meta/fbyv2.json`, all carrying thresholds, so all three
        generate. Every other count held, which is what showed it was a gain and
        not a reshuffle between the ledger's columns.

        68 -> 106: three `meta/bletchley/` configurations were vendored, to make
        a real coverage diff reproducible from a clone. This move is the opposite
        shape -- every column grows, because a whole platform arrived rather than
        a handful of channels on files already here.

        106 -> 180: two causes in one change, and they are worth separating.

        The smaller one is another vendored file, `ampere/mtjade.json`, added for
        the PSU input/output power pair a conservation check needs.

        The larger one is a reader fix, and it has the same shape as the 65 -> 68
        move: the rail set is now taken from the `Labels` array that DECLARES it
        rather than from the thresholds, which are a proxy for it. Across this
        corpus `Labels` declares 149 rails and 34 carry a threshold; the other 115
        were never constructed, so nothing expected them and their absence could
        never be reported.

        **`excluded_no_thresholds` grows hardest -- 19 -> 137 -- and that is the
        honest shape of the fix.** Most newly-visible rails carry no bounds, so
        they are declared, counted, and then excluded from the MODEL with a stated
        reason. Being excluded for a reason is the difference this whole ledger
        exists to record: before, they were absent from it entirely.
        """
        _, _, manifest = built
        counts = manifest.counts()
        assert counts["generated"] == 180
        assert counts["excluded_templated_name"] == 24
        assert counts["excluded_not_a_sensor"] == 36
        assert counts["excluded_no_thresholds"] == 137
        assert counts["with_lower_bound"] == 137


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
    """AC4: a declared floor reaches the engine as a floor.

    These used to assert the negation transform -- a mirrored `reading_low`
    indicator carrying `-value` against `-threshold`, because BOUNDEDNESS was
    upper-bound-only. Engine 0.1.7 takes floors natively, so the mechanism is gone
    and these assert what it was FOR: the number the machine declared, on the
    correct side, not lost and not sign-flipped. A test written as the old
    limitation would now be a pin holding a falsehood.
    """

    def test_a_declared_floor_reaches_the_model_as_a_floor(self, built):
        _, model, manifest = built
        with_lower = [s for s in manifest.sensors if s.has_lower]
        assert with_lower
        for sensor in with_lower:
            indicators = model["domain"]["indicators"][sensor.entity_type]
            assert len(indicators) == 1, (
                f"{sensor.declared_name} has {len(indicators)} indicators; the "
                f"mirrored one should have been retired")
            keys = indicators[0]
            assert "lower_critical" in keys or "lower_warning" in keys, (
                f"{sensor.declared_name} lost its lower bound")

    def test_a_floor_is_the_declared_number_not_its_negation(self, built):
        """The sign is the whole regression risk in retiring the transform: a
        leftover negation would read as a floor of -500 for a fan declared at 500,
        which no reading can ever fall below."""
        _, model, manifest = built
        sensor = next(s for s in manifest.sensors
                      if s.lower[1] is not None and s.lower[0] is not None)
        indicator = model["domain"]["indicators"][sensor.entity_type][0]
        assert indicator["lower_warning"] == sensor.lower[0]
        assert indicator["lower_critical"] == sensor.lower[1]

    def test_a_floor_is_never_above_its_own_ceiling(self, built):
        """A band whose floor sits at or above its ceiling is declined by the engine
        once as `missing_config` rather than firing forever, so a generator that
        emitted one would silently stop checking that sensor."""
        _, model, manifest = built
        for sensor in manifest.sensors:
            i = model["domain"]["indicators"][sensor.entity_type][0]
            if "lower_critical" in i and "critical" in i:
                assert i["lower_critical"] < i["critical"], (
                    f"{sensor.declared_name}: floor {i['lower_critical']} is not "
                    f"below ceiling {i['critical']}")

    def test_no_mirrored_indicator_survives_anywhere(self, built):
        """Set equality against the whole model, not a spot check. A leftover
        mirror would feed negated values nothing reads and inflate the denominator
        with invariants that can only decline."""
        _, model, _ = built
        names = {i["name"] for inds in model["domain"]["indicators"].values()
                 for i in inds}
        assert names == {READING}, f"unexpected indicator names: {sorted(names)}"

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
    """The engine names the sanitised entity type; an operator knows the name on the
    board. Translation is now only that -- it stopped being un-negation when the
    engine started saying *below* itself."""

    @pytest.mark.parametrize("problem_type", [
        "below_critical_threshold", "below_warning_threshold", "approaching_floor"])
    def test_every_floor_side_finding_reads_as_below(self, built, problem_type):
        _, _, manifest = built
        sensor = next(s for s in manifest.sensors if s.lower[1] is not None)
        finding = {"entity_id": sensor.entity_type, "severity": "critical",
                   "problem_type": f"{problem_type}:{READING}",
                   "reason": f"{READING} is below critical threshold"}
        translated = manifest.translate_finding(finding)
        assert "BELOW" in translated
        assert sensor.declared_name in translated

    @pytest.mark.parametrize("problem_type", [
        "threshold_exceeded", "threshold_warning", "approaching_limit"])
    def test_every_ceiling_side_finding_reads_as_above(self, built, problem_type):
        _, _, manifest = built
        sensor = next(s for s in manifest.sensors if s.upper[1] is not None)
        finding = {"entity_id": sensor.entity_type, "severity": "critical",
                   "problem_type": f"{problem_type}:{READING}",
                   "reason": f"{READING} exceeds critical threshold"}
        assert "above" in manifest.translate_finding(finding)

    def test_the_bound_table_is_the_engines_vocabulary_not_ours(self):
        """Derived, not transcribed. Every BOUNDEDNESS problem_type the installed
        engine can emit must be classified, or a finding lands on the wrong side of
        the band with full confidence.

        Read off the checker's source rather than off a probe, because a probe only
        shows the arms it managed to trigger -- which is how an arm gets missed.
        """
        boundedness = pytest.importorskip(
            "arbiter_engine.ontology.axioms.boundedness",
            reason="engine is the optional [detect] extra")
        source = pathlib.Path(boundedness.__file__).read_text()
        emitted = set(re.findall(r"problem_type=f?['\"]([a-z_]+):", source))
        unclassified = emitted - set(BOUND_OF_PROBLEM)
        assert not unclassified, (
            f"BOUNDEDNESS can emit {sorted(unclassified)}, which this build does not "
            f"map to a side of the band; a finding of that shape would be reported "
            f"with the engine's own words and no direction")

    def test_an_unknown_problem_type_is_not_filed_under_either_side(self, built):
        """The other half. Guessing between `above` and `BELOW` on an unrecognised
        shape is the confident misclassification this project keeps finding."""
        _, _, manifest = built
        sensor = manifest.sensors[0]
        finding = {"entity_id": sensor.entity_type, "severity": "warning",
                   "problem_type": f"some_future_arm:{READING}",
                   "reason": f"{READING} did something new"}
        translated = manifest.translate_finding(finding)
        assert "BELOW" not in translated and "above its upper" not in translated
        assert sensor.declared_name in translated

    def test_a_liveness_finding_is_not_rendered_as_a_threshold_breach(self, built):
        """The engine has more than one finding shape. Treating every one as a bound
        breach printed `3VSB is above its upper high bound of 3.52` for a sensor
        sitting at 3.35 whose series had simply stopped moving -- a real number under
        the wrong name, which is worse than no number."""
        _, _, manifest = built
        sensor = manifest.sensors[0]
        finding = {"entity_id": sensor.entity_type, "severity": "high",
                   "problem_type": "frozen_series:reading", "axiom": "STABILITY",
                   "reason": "reading has not changed across 14 of 30 observations"}
        translated = manifest.translate_finding(finding)
        assert sensor.declared_name in translated
        assert "has not changed" in translated
        assert "bound" not in translated

    def test_an_unrecognised_severity_omits_the_bound_rather_than_guessing(self, built):
        """`severity` is the engine's vocabulary, not ours -- `high` arrives alongside
        `warning` and `critical`. Mapping an unknown one onto the nearest slot names a
        threshold the finding is not about."""
        _, _, manifest = built
        sensor = next(s for s in manifest.sensors if s.upper[1] is not None)
        finding = {"entity_id": sensor.entity_type, "severity": "high",
                   "problem_type": f"threshold_exceeded:{READING}", "reason": "x"}
        translated = manifest.translate_finding(finding)
        assert "high bound" in translated
        assert str(sensor.upper[0]) not in translated
        assert str(sensor.upper[1]) not in translated

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

    The pair needs PyYAML, which Stage 1 does not: the artifact these rules have to
    police is the SERIALISED model, so the check needs the serialiser. Ungated, that
    import failed on any interpreter without PyYAML — including the CI runner, which
    installs nothing on purpose, so this pair was failing there from the commit that
    introduced it.

    The gate below is therefore only half the fix. A skip here would replace a leak
    check with silence in the one environment CI actually runs, which is worse than
    the failure it removes, so the workflow installs PyYAML for the test step. If
    that ever stops being true, this becomes two quiet skips and nothing says so.
    """

    @pytest.fixture(autouse=True)
    def _needs_the_serialiser(self):
        pytest.importorskip(
            "yaml",
            reason="the hygiene rules police the serialised model, so this pair "
                   "needs PyYAML; CI installs it, and the [detect] extra brings it")

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


class TestTheRetiredMechanismLeavesOnlyItsExplanation:
    """A negative claim that was false because of the sentence making it.

    The generator's docstring tells a reviewer to grep `READING_LOW` rather than
    `neg`, because a word-level grep counts prose about a removal as an instance of
    the thing removed -- four consecutive reviews reported the transform as still
    present on exactly that evidence.

    The first draft of that advice said the symbol was "absent from the whole
    package", and naming it made that false: the sentence became the only
    occurrence. So the claim under test is not *the symbol never appears*, which is
    unmaintainable, but the one that matters -- **no code uses it**.

    Docstrings are stripped before asserting, which is the same discipline a
    `literal not in source` assertion has needed here before.
    """

    @staticmethod
    def _source_without_docstrings(path):
        """The module's text with every docstring removed.

        Walks the AST rather than pattern-matching quotes: a regex for triple-quoted
        blocks is defeated by nested quotes and by a string that merely looks like a
        docstring, and this assertion is only worth making if it is exact.
        """
        import ast
        text = path.read_text()
        tree = ast.parse(text)
        spans = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                continue
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                spans.append((first.lineno, first.end_lineno))
        lines = text.splitlines()
        kept = [line for number, line in enumerate(lines, start=1)
                if not any(start <= number <= end for start, end in spans)]
        return "\n".join(kept)

    def test_no_code_uses_the_retired_symbol(self):
        src = ROOT / "src" / "bmc_sensor_audit"
        offenders = []
        for path in sorted(src.rglob("*.py")):
            if "READING_LOW" in self._source_without_docstrings(path):
                offenders.append(str(path.relative_to(ROOT)))
        assert offenders == [], (
            f"{offenders} use READING_LOW outside a docstring; the mirrored "
            f"indicator was retired and nothing should reference it")

    def test_the_retired_symbol_survives_only_here(self):
        """One hit, in the paragraph that explains it. Nought would be ambiguous
        between *retired* and *you mistyped the symbol*."""
        src = ROOT / "src" / "bmc_sensor_audit"
        hits = {str(p.relative_to(ROOT)): p.read_text().count("READING_LOW")
                for p in sorted(src.rglob("*.py")) if "READING_LOW" in p.read_text()}
        assert hits == {"src/bmc_sensor_audit/detect/generator.py": 1}, hits

    def test_the_docstring_stripper_actually_strips(self):
        """Non-vacuity. A stripper that returned the whole file would make the
        assertion above pass by never removing anything, and a stripper that
        returned nothing would make it pass by having nothing to find."""
        path = ROOT / "src" / "bmc_sensor_audit" / "detect" / "generator.py"
        stripped = self._source_without_docstrings(path)
        assert "If you got here by grepping" not in stripped, "nothing was stripped"
        assert "def generate(" in stripped, "the stripper removed code as well"
