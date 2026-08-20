"""The firmware before/after gate: which sensors did this update remove or rename?

**Running the coverage diff twice does not answer this question**, and the first
test says so with a measurement rather than an assertion about intent. A firmware
that renames `FAN0` to `Fan 0` still matches the declaration through the
normalised matcher, so both coverage runs come back identical while every alert
rule keyed on the old string goes quiet.

The other load-bearing case is `test_an_incomplete_after_walk_withholds_absence`.
A partial walk renders as a firmware that deleted the machine -- the single most
alarming way to report a network timeout, and the one a gate must never produce.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bmc_sensor_audit.inventory.diff import compare
from bmc_sensor_audit.inventory.entity_manager import load_declaration
from bmc_sensor_audit.inventory.redfish import RedfishClient, walk_chassis, walk_from_dict
from bmc_sensor_audit.inventory.regression import compare_walks
from bmc_sensor_audit.report import regression_as_json, regression_as_text
from bmc_sensor_audit.testing.mock_redfish import MockBMC, serve

FIXTURES = Path(__file__).parent / "fixtures"


def _walk(bmc: MockBMC):
    with serve(bmc) as url:
        return walk_chassis(RedfishClient(url))


def _kinds(report) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for change in report.changes:
        out.setdefault(change.kind, []).append(change.sensor)
    return out


@pytest.fixture
def before():
    bmc = MockBMC(shape="sensors")
    bmc.add("FAN0", reading=4200.0, units="RPM",
            upper_critical=9000.0, lower_critical=1000.0)
    bmc.add("P12V", reading=12.1, units="V", upper_critical=13.2)
    bmc.add("INLET_TEMP", reading=24.0, upper_critical=45.0)
    bmc.add("OUTLET_TEMP", reading=31.0, upper_critical=55.0)
    return _walk(bmc)


class TestTheCoverageDiffCannotAnswerThis:
    def test_a_rename_is_invisible_to_two_coverage_runs(self, tmp_path):
        """The measurement behind the module docstring, not a claim about it.

        Bletchley declares `FAN0_TACH_IL`. A firmware serving `FAN0 TACH IL`
        instead still matches it -- the normalised matcher exists precisely to
        tolerate that spelling difference -- so the declaration diff reports the
        same thing before and after, while the name every consumer keys on has
        changed underneath.
        """
        config = FIXTURES / "upstream" / "meta" / "bletchley"
        declaration = load_declaration([str(config)])
        assert any(s.display_name == "FAN0_TACH_IL" for s in declaration.sensors)

        old = MockBMC(shape="sensors")
        old.add("FAN0_TACH_IL", reading=4200.0, units="RPM")
        new = MockBMC(shape="sensors")
        new.add("FAN0 TACH IL", reading=4300.0, units="RPM")

        before_walk, after_walk = _walk(old), _walk(new)
        first = compare(declaration, before_walk)
        second = compare(declaration, after_walk)
        assert first.counts()["matched"] == second.counts()["matched"] == 1

        # The gate does not move. Identical exit code, identical regressions --
        # so a pipeline gated on this is green across the rename.
        assert first.exit_code == second.exit_code
        assert ([f.kind for f in first.regressions]
                == [f.kind for f in second.regressions])

        # The one trace is an advisory that does not fail anything, and cannot
        # say what it means: `matched_inexactly` fires the same way for a name
        # that was always spelled differently as for one renamed this morning.
        difference = ([f.kind for f in second.findings]
                      != [f.kind for f in first.findings])
        assert difference
        extra = [f for f in second.findings if f.kind == "matched_inexactly"]
        assert extra and not any(f.is_regression for f in extra)

        # The walk-to-walk comparison is where the rename becomes a finding that
        # names both spellings and fails the gate.
        report = compare_walks(before_walk, after_walk)
        assert "sensor_renamed" in _kinds(report)
        assert report.regressions


class TestWhatTheGateFinds:
    def test_a_removed_sensor(self, before):
        bmc = MockBMC(shape="sensors")
        bmc.add("FAN0", reading=4200.0, units="RPM",
                upper_critical=9000.0, lower_critical=1000.0)
        report = compare_walks(before, _walk(bmc))
        assert "OUTLET_TEMP" in _kinds(report)["sensor_removed"]
        assert report.regressions

    def test_a_rename_at_the_same_uri(self, before):
        bmc = MockBMC(shape="sensors")
        bmc.add("Fan 0", reading=4200.0, units="RPM",
                upper_critical=9000.0, lower_critical=1000.0)
        report = compare_walks(before, _walk(bmc))
        renamed = [c for c in report.changes if c.kind == "sensor_renamed"]
        assert len(renamed) == 1
        assert "'FAN0'" in renamed[0].detail and "'Fan 0'" in renamed[0].detail

    def test_a_threshold_that_moved_and_one_that_vanished(self, before):
        bmc = MockBMC(shape="sensors")
        bmc.add("FAN0", reading=4200.0, units="RPM", upper_critical=9000.0)
        bmc.add("P12V", reading=12.1, units="V", upper_critical=13.8)
        report = compare_walks(before, _walk(bmc))
        kinds = _kinds(report)
        assert kinds["threshold_removed"] == ["FAN0"]
        assert kinds["threshold_moved"] == ["P12V"]

    def test_a_sensor_that_stopped_reading_while_still_enabled(self, before):
        bmc = MockBMC(shape="sensors")
        bmc.add("FAN0", reading=None, units="RPM",
                upper_critical=9000.0, lower_critical=1000.0)
        report = compare_walks(before, _walk(bmc))
        assert _kinds(report)["reading_lost"] == ["FAN0"]

    def test_a_sensor_switched_off(self, before):
        bmc = MockBMC(shape="sensors")
        bmc.add("FAN0", reading=4200.0, units="RPM",
                upper_critical=9000.0, lower_critical=1000.0)
        bmc.disable("FAN0")
        report = compare_walks(before, _walk(bmc))
        assert _kinds(report)["sensor_disabled"] == ["FAN0"]

    def test_units_changing_under_a_stable_name(self, before):
        bmc = MockBMC(shape="sensors")
        bmc.add("P12V", reading=12100.0, units="mV", upper_critical=13.2)
        report = compare_walks(before, _walk(bmc))
        assert _kinds(report)["units_changed"] == ["P12V"]

    def test_an_added_sensor_is_reported_and_is_not_a_regression(self, before):
        bmc = MockBMC(shape="sensors")
        for sensor in before:
            bmc.add(sensor.name, reading=sensor.reading, units=sensor.units or "Cel")
        bmc.add("NEW_TEMP", reading=20.0)
        report = compare_walks(before, _walk(bmc))
        added = [c for c in report.changes if c.kind == "sensor_added"]
        assert [c.sensor for c in added] == ["NEW_TEMP"]
        assert not any(c.is_regression for c in added)

    def test_a_clean_reflash_reports_nothing(self, before):
        bmc = MockBMC(shape="sensors")
        bmc.add("FAN0", reading=4300.0, units="RPM",
                upper_critical=9000.0, lower_critical=1000.0)
        bmc.add("P12V", reading=12.0, units="V", upper_critical=13.2)
        bmc.add("INLET_TEMP", reading=25.0, upper_critical=45.0)
        bmc.add("OUTLET_TEMP", reading=32.0, upper_critical=55.0)
        report = compare_walks(before, _walk(bmc))
        assert report.changes == [], [str(c) for c in report.changes]
        assert "No changes" in regression_as_text(report, before="a", after="b")

    def test_a_reading_that_merely_changed_value_is_not_a_change(self, before):
        """Every reading moves between two walks. Reporting that would drown the
        report in the one thing a working board does constantly."""
        bmc = MockBMC(shape="sensors")
        for sensor in before:
            bmc.add(sensor.name, reading=(sensor.reading or 0) + 1,
                    units=sensor.units or "Cel",
                    upper_critical=sensor.thresholds.get(("upper", "critical")),
                    lower_critical=sensor.thresholds.get(("lower", "critical")))
        assert compare_walks(before, _walk(bmc)).changes == []


class TestWhatItRefusesToGuess:
    def test_a_positional_uri_scheme_does_not_manufacture_renames(self, before):
        """Inserting one sensor shifts every URI after it on a positional scheme.

        Measured, not hypothesised: pairing on URI alone reported two confident
        renames on a firmware that had renamed nothing. The URI has to be
        corroborated by something a rename would not change -- units and resource
        type -- and the sensor that merely inherited a position is measuring
        something else.
        """
        bmc = MockBMC(shape="sensors")
        bmc.add("SPACER", reading=1.0)          # shifts every generated URI
        bmc.add("Fan 0", reading=4200.0, units="RPM")
        report = compare_walks(before, _walk(bmc))
        kinds = _kinds(report)
        assert "sensor_renamed" not in kinds, (
            "a URI is not an identity on a positional scheme")
        assert "FAN0" in kinds["sensor_removed"]
        assert "Fan 0" in kinds["sensor_added"]
        assert "does not guess" in regression_as_text(report, before="a", after="b")

    def test_a_rename_that_also_changed_units_is_two_changes(self, before):
        """The corroboration is gone, so the evidence tying them together is too."""
        bmc = MockBMC(shape="sensors")
        bmc.add("Fan 0", reading=70.0, units="Percent",
                upper_critical=9000.0, lower_critical=1000.0)
        kinds = _kinds(compare_walks(before, _walk(bmc)))
        assert "sensor_renamed" not in kinds
        assert "FAN0" in kinds["sensor_removed"] and "Fan 0" in kinds["sensor_added"]

    def test_an_incomplete_after_walk_withholds_absence(self, before):
        """The load-bearing test. A transport failure must never render as a
        firmware that deleted the board."""
        bmc = MockBMC(shape="sensors", fail={"/redfish/v1/Chassis/1/Sensors": 500})
        broken = _walk(bmc)
        assert not broken.complete

        report = compare_walks(before, broken)
        assert report.absence_withheld
        assert not any(c.kind in ("sensor_removed", "sensor_added")
                       for c in report.changes)
        assert "walk_incomplete" in _kinds(report)

    def test_an_incomplete_walk_exits_could_not_complete_not_regression(self, before,
                                                                       tmp_path, capsys):
        from bmc_sensor_audit.cli import main

        bmc = MockBMC(shape="sensors", fail={"/redfish/v1/Chassis/1/Sensors": 500})
        first = tmp_path / "before.json"
        second = tmp_path / "after.json"
        first.write_text(json.dumps(before.to_dict()))
        second.write_text(json.dumps(_walk(bmc).to_dict()))
        assert main(["regression", "--before", str(first), "--after", str(second)]) == 2

    def test_field_drift_is_not_computed_against_a_capture_that_never_looked(self, before):
        """One side predates recording object properties, so drift is unknowable.

        Silence would be the wrong answer twice over: it reads as no drift, and it
        reads as a check that ran.
        """
        old = walk_from_dict(
            json.loads((FIXTURES / "walk_qemu_bletchley.json").read_text()))
        report = compare_walks(old, before)
        assert not report.fields_comparable
        assert "field_drift" not in _kinds(report)
        assert "not computed" in regression_as_text(report, before="a", after="b")

    def test_field_drift_is_reported_when_both_walks_looked(self, before):
        bmc = MockBMC(shape="sensors")
        for sensor in before:
            bmc.add(sensor.name, reading=sensor.reading, units=sensor.units or "Cel",
                    upper_critical=sensor.thresholds.get(("upper", "critical")),
                    lower_critical=sensor.thresholds.get(("lower", "critical")),
                    extra={"ThermalZone": "front"} if sensor.name == "INLET_TEMP" else {})
        report = compare_walks(before, _walk(bmc))
        assert report.fields_comparable
        drift = [c for c in report.changes if c.kind == "field_drift"]
        assert [c.sensor for c in drift] == ["INLET_TEMP"]
        assert not drift[0].is_regression, (
            "a vendor extension is permitted by the standard; a gate that failed "
            "on the first one gets switched off, taking the signal with it")


class TestTheVocabularyStaysWhole:
    """Three lists name change kinds: what the module emits, what the report ranks,
    and what it gives a headline to. Restating a set in three places is how the
    copies come to disagree, so the emitted set is read out of the source rather
    than typed here -- a kind added tomorrow appears in this test on its own.
    """

    @staticmethod
    def _emitted() -> set[str]:
        import re

        from bmc_sensor_audit.inventory import regression as module

        source = Path(module.__file__).read_text()
        kinds = set(re.findall(r'Change\(\s*"([a-z_]+)"', source))
        assert kinds, "no change kinds found in the source; the pattern moved"
        return kinds

    def test_every_kind_is_ranked_and_has_a_headline(self):
        from bmc_sensor_audit.report import CHANGE_ORDER, _CHANGE_HEADLINE

        emitted = self._emitted()
        assert emitted - set(CHANGE_ORDER) == set(), "unranked kinds sort last silently"
        assert emitted - set(_CHANGE_HEADLINE) == set(), "kinds with no headline"

    def test_nothing_is_ranked_that_cannot_be_emitted(self):
        """The other direction. A stale entry is not dangerous, but it is a claim
        that the report can produce something it cannot."""
        from bmc_sensor_audit.report import CHANGE_ORDER

        assert set(CHANGE_ORDER) - self._emitted() == set()

    def test_every_regression_kind_is_one_the_module_emits(self):
        from bmc_sensor_audit.inventory.regression import REGRESSION_KINDS

        assert set(REGRESSION_KINDS) - self._emitted() == set(), (
            "a kind listed as a regression that nothing produces cannot fail a "
            "gate, and reads as coverage that is not there")


class TestDriftCanBeRequired:
    """`--strict-fields` on `regression`: the flag that gives drift a handle.

    **Reported from outside, as the sibling of the coverage finding.** Drift is
    computed opportunistically when both walks happen to carry observations, and
    the report says so when they do not -- but until this flag existed that was
    all it could do. A pipeline gating firmware on `regression` and needing drift
    covered had no way to ask for it: the run printed *not computed* and exited on
    the strength of the comparisons that did run.

    **A flag, not a default, and the weight is the argument.** Flooring flagless
    would turn every regression run against an older baseline into exit 2 and
    break the removal and rename gating that works perfectly well on those
    captures. So drift stays best-effort until somebody asks, and asking is what
    makes the existing could-not-complete rule apply.

    The flag is spelled as it is on `coverage` because it means the same sentence
    -- apply field strictness, and require it to be applicable. Two flags spelled
    alike that mean different things would be a worse defect than two names, so
    this one also prints the after-walk's own strictness, exactly as `coverage`
    prints the walk's.
    """

    @staticmethod
    def _old() -> Path:
        return FIXTURES / "walk_qemu_bletchley.json"

    def _fresh(self, tmp_path: Path, name: str, **kwargs) -> Path:
        bmc = MockBMC(shape="sensors")
        bmc.add("Inlet", reading=20.0, **kwargs)
        path = tmp_path / name
        path.write_text(json.dumps(_walk(bmc).to_dict()))
        return path

    def test_without_the_flag_nothing_changes(self, tmp_path):
        """The behaviour every existing user has. Two pre-property captures with
        no changes between them still exit 0."""
        from bmc_sensor_audit.cli import main

        old = tmp_path / "old.json"
        old.write_text(self._old().read_text())
        same = tmp_path / "same.json"
        same.write_text(self._old().read_text())
        assert main(["regression", "--before", str(old), "--after", str(same)]) == 0

    def test_with_the_flag_an_uncomparable_run_exits_2(self, tmp_path):
        from bmc_sensor_audit.cli import main

        old = tmp_path / "old.json"
        old.write_text(self._old().read_text())
        same = tmp_path / "same.json"
        same.write_text(self._old().read_text())
        assert main(["regression", "--before", str(old), "--after", str(same),
                     "--strict-fields"]) == 2

    def test_one_stale_side_is_enough_and_the_message_names_it(self, tmp_path, capsys):
        """The fix differs by side: one old capture means re-capture that one."""
        from bmc_sensor_audit.cli import main

        old = tmp_path / "old.json"
        old.write_text(self._old().read_text())
        fresh = self._fresh(tmp_path, "after.json")
        assert main(["regression", "--before", str(old), "--after", str(fresh),
                     "--strict-fields"]) == 2
        err = capsys.readouterr().err
        assert "--before" in err and "--after:" not in err
        assert "one of the two captures" in err

    def test_two_fresh_captures_exit_on_their_content(self, tmp_path):
        """The flag adds a requirement, not a verdict. Two walks that both looked
        are judged by what changed between them and nothing else."""
        from bmc_sensor_audit.cli import main

        before = self._fresh(tmp_path, "before.json")
        after = self._fresh(tmp_path, "after.json")
        assert main(["regression", "--before", str(before), "--after", str(after),
                     "--strict-fields"]) == 0

        drifted = self._fresh(tmp_path, "drifted.json", extra={"VendorZone": "a"})
        # Drift arrived, and arriving drift is still not a regression.
        assert main(["regression", "--before", str(before), "--after", str(drifted),
                     "--strict-fields"]) == 0

    def test_the_flag_also_prints_the_after_walks_own_strictness(self, tmp_path, capsys):
        """What makes it the same flag rather than a name collision.

        `field_drift` above names what ARRIVED; this names what the firmware
        carries now, which is what a downstream parser actually meets.
        """
        from bmc_sensor_audit.cli import main

        before = self._fresh(tmp_path, "before.json", extra={"VendorZone": "a"})
        after = self._fresh(tmp_path, "after.json", extra={"VendorZone": "a"})
        main(["regression", "--before", str(before), "--after", str(after),
              "--strict-fields"])
        out = capsys.readouterr().out
        assert "Field strictness" in out
        assert "VendorZone" in out
        # Carried by both walks, so it did not arrive and is not a change.
        assert "field_drift" not in out

    def test_a_real_regression_still_outranks_nothing_and_is_not_masked(self, tmp_path):
        """2 over 1 here as everywhere: a run with removals AND an unaskable drift
        question has not finished asking."""
        from bmc_sensor_audit.cli import main

        old = tmp_path / "old.json"
        old.write_text(self._old().read_text())
        emptied = tmp_path / "emptied.json"
        payload = json.loads(self._old().read_text())
        payload["sensors"] = payload["sensors"][:1]
        emptied.write_text(json.dumps(payload))

        assert main(["regression", "--before", str(old), "--after", str(emptied)]) == 1
        assert main(["regression", "--before", str(old), "--after", str(emptied),
                     "--strict-fields"]) == 2


class TestTheCommandLine:
    def test_captures_in_the_wrong_order_are_refused_not_swapped(self, tmp_path, capsys):
        """A reversed comparison reports every removal as an addition, which reads
        like a clean upgrade. So it stops rather than quietly reordering."""
        from bmc_sensor_audit.cli import main

        bmc = MockBMC(shape="sensors")
        bmc.add("FAN0", reading=1.0)
        walk = _walk(bmc)

        older = walk.to_dict()
        older["captured_at"] = "2026-01-01T00:00:00+00:00"
        newer = walk.to_dict()
        newer["captured_at"] = "2026-06-01T00:00:00+00:00"

        first, second = tmp_path / "old.json", tmp_path / "new.json"
        first.write_text(json.dumps(newer))       # deliberately the wrong way round
        second.write_text(json.dumps(older))

        assert main(["regression", "--before", str(first), "--after", str(second)]) == 2
        assert "wrong way round" in capsys.readouterr().err

    def test_the_json_view_carries_every_change(self, before, tmp_path):
        bmc = MockBMC(shape="sensors")
        bmc.add("FAN0", reading=4200.0, units="RPM", upper_critical=9000.0)
        report = compare_walks(before, _walk(bmc))
        payload = json.loads(regression_as_json(report, before="a", after="b"))
        assert payload["regressions"] == len(report.regressions)
        assert len(payload["changes"]) == len(report.changes)
        assert payload["fields_comparable"] is True
        assert {c["kind"] for c in payload["changes"]} == set(_kinds(report))
