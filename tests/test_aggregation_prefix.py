"""A declared aggregation prefix, and the shift this tool notices without one.

A BMC that aggregates a satellite controller republishes its resources under a
prefix. When that prefix changes across a firmware or topology update, every name
behind it moves at once — and `regression` reads that as a mass removal plus a mass
addition, which fails a gate on a machine where nothing was lost.

**The fix is a declaration, not a heuristic.** `--aggregation-prefix OLD=NEW` is the
operator stating that two subtrees are the same one. The pairing it produces is
annotated with the claim it rests on, and the pairing is the only thing the flag
does: with the flag absent, nothing auto-pairs.

**What the tool does on its own is notice the shape and name it.** A set of names
sharing one leading string vanishing while an identically-shaped set sharing another
appears is reported, with both prefixes quoted and the flag that would declare
them — and the removals stay removals, so the gate still fails. Surfaced, not
assumed, which is the same rule the rename pass follows one door over.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bmc_sensor_audit.inventory.redfish import (  # noqa: E402
    WALK_FORMAT, RedfishClient, walk_chassis, walk_from_dict)
from bmc_sensor_audit.inventory.regression import (  # noqa: E402
    REGRESSION_KINDS, compare_walks, parse_prefix_map)
from bmc_sensor_audit.testing.mock_redfish import MockBMC, serve  # noqa: E402

HOST = (("INLET_TEMP", 24.0), ("FAN0", 4200.0))
SATELLITE = (("TEMP0", 41.0), ("TEMP1", 43.0), ("POWER", 310.0), ("VOLT", 12.1))


def _walk(prefix: str, *, rename_one: bool = False, drop: str = "",
          drop_threshold: str = ""):
    """A host board plus a four-sensor satellite republished behind `prefix`.

    **Built as a `walk/1` payload rather than served by the mock, and that is a
    correction rather than a shortcut.** The first version of this fixture used
    `MockBMC`, which numbers every sensor positionally inside one chassis --
    `/Sensors/s0`, `/Sensors/s1` -- so the URI did not move when the prefix did.
    The URI pass then paired all four across the change and reported four renames,
    and the mass removal this whole feature exists for never appeared.

    An aggregation prefix reaches the URI: bmcweb republishes a satellite's
    resources under a prefixed chassis. So the fixture puts it there. The
    name-only variant is a real firmware too, and it has its own class below.

    The host sensors carry no prefix and never move. They are the control group:
    a pass that paired everything by accident shows up there.
    """
    sensors = []
    for index, (name, reading) in enumerate(HOST):
        sensors.append({"name": name, "reading": reading, "units": "Cel",
                        "path": f"/redfish/v1/Chassis/1/Sensors/s{index}",
                        "shape": "sensors", "resource": "Sensor",
                        "thresholds": {"upper/critical": 100.0}})
    for index, (name, reading) in enumerate(SATELLITE):
        if rename_one and name == "TEMP1":
            name = "TEMP_1"
        if drop and name == drop:
            continue
        thresholds = {} if name == drop_threshold else {"upper/critical": 100.0}
        sensors.append({"name": f"{prefix}{name}", "reading": reading, "units": "Cel",
                        "path": f"/redfish/v1/Chassis/{prefix}Sat/Sensors/s{index}",
                        "shape": "sensors", "resource": "Sensor",
                        "thresholds": thresholds})
    return walk_from_dict({"format": WALK_FORMAT, "fields_observed": True,
                           "shapes_seen": ["sensors"], "errors": [],
                           "chassis": ["/redfish/v1/Chassis/1"],
                           "sensors": sensors})


def _kinds(report) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for change in report.changes:
        out.setdefault(change.kind, []).append(change.sensor)
    return out


@pytest.fixture(scope="module")
def before():
    return _walk("HMC_0_")


@pytest.fixture(scope="module")
def after():
    return _walk("GPU_0_")


class TestWithoutTheDeclarationNothingPairs:
    def test_a_prefix_change_reads_as_mass_removal(self, before, after):
        """The problem, measured rather than asserted. This is what a firmware that
        changed nothing but its aggregation prefix looks like today."""
        report = compare_walks(before, after)
        kinds = _kinds(report)
        assert len(kinds["sensor_removed"]) == 4
        assert len(kinds["sensor_added"]) == 4
        assert report.regressions, "a prefix change must not read as clean"

    def test_the_shift_is_reported_and_names_both_prefixes(self, before, after):
        report = compare_walks(before, after)
        shift = _kinds(report)["aggregation_prefix_shift"]
        assert shift == ["(topology)"]
        detail = next(c.detail for c in report.changes
                      if c.kind == "aggregation_prefix_shift")
        assert "'HMC_0_'" in detail and "'GPU_0_'" in detail
        assert "--aggregation-prefix HMC_0_=GPU_0_" in detail, (
            "naming the prefix without naming the flag leaves the reader to "
            "work out the fix from prose")

    def test_the_shift_report_does_not_fail_the_gate_by_itself(self):
        """It is a description of a shape, not a finding. The removals it explains
        are the regressions, and they are still there."""
        assert "aggregation_prefix_shift" not in REGRESSION_KINDS

    def test_the_host_sensors_are_untouched(self, before, after):
        """The control group. A pass that paired everything by accident, or one that
        rewrote names it should not have, shows up here."""
        report = compare_walks(before, after)
        moved = {name for names in _kinds(report).values() for name in names}
        assert "INLET_TEMP" not in moved and "FAN0" not in moved


class TestTheShiftReportRefusesToGuess:
    def test_one_sensor_changing_name_is_not_a_prefix_shift(self):
        """A single rename is a rename, and there is already a pass for it. Two is
        the floor because one name pair has a common prefix by construction."""
        report = compare_walks(_walk("HMC_0_", drop="TEMP0"),
                               _walk("HMC_0_", drop="TEMP1"))
        assert "aggregation_prefix_shift" not in _kinds(report)

    def test_a_mixed_removal_set_suppresses_the_report(self):
        """A genuinely removed host sensor sitting alongside a shifted subtree
        collapses the common prefix, and nothing is reported. That is a miss rather
        than a wrong answer, and it is the right way round: partitioning a mixed set
        of removals into subtrees is the guess this refuses to make."""
        after = _walk("GPU_0_")
        after.sensors = [s for s in after.sensors if s.name != "FAN0"]
        report = compare_walks(_walk("HMC_0_"), after)
        assert "sensor_removed" in _kinds(report)
        assert "aggregation_prefix_shift" not in _kinds(report)

    def test_identical_walks_report_no_shift(self):
        """Non-vacuity for the whole detector: it fires on a shape, and a machine
        that did not change has no shape to fire on."""
        walk = _walk("HMC_0_")
        assert "aggregation_prefix_shift" not in _kinds(compare_walks(walk, walk))


class TestTheDeclaredMapPairs:
    def test_a_prefix_only_change_pairs_with_a_note(self, before, after):
        report = compare_walks(before, after, prefix_map=[("HMC_0_", "GPU_0_")])
        kinds = _kinds(report)
        assert "sensor_removed" not in kinds
        assert "sensor_added" not in kinds
        assert len(kinds["aggregation_prefix_paired"]) == 4
        assert report.prefix_paired == 4

    def test_a_declared_prefix_change_is_not_a_regression(self, before, after):
        """The whole point of the flag: a mass false regression on a machine that
        lost nothing is how a gate teaches people to switch it off."""
        report = compare_walks(before, after, prefix_map=[("HMC_0_", "GPU_0_")])
        assert report.regressions == []

    def test_the_pairing_is_annotated_with_the_claim_it_rests_on(self, before, after):
        report = compare_walks(before, after, prefix_map=[("HMC_0_", "GPU_0_")])
        detail = next(c.detail for c in report.changes
                      if c.kind == "aggregation_prefix_paired")
        assert "declared prefix map" in detail
        assert "nothing here verified it" in detail

    def test_the_paired_count_separates_declared_from_measured(self, before, after):
        """`paired` on its own would report a firmware as fully accounted for on the
        strength of a flag nobody checked."""
        report = compare_walks(before, after, prefix_map=[("HMC_0_", "GPU_0_")])
        assert report.paired == 6
        assert report.prefix_paired == 4

    def test_a_declared_map_that_matches_nothing_changes_nothing(self, before, after):
        """A typo in the prefix is not silently a no-op-that-looks-like-success: the
        report is exactly the undeclared one, mass removals and all."""
        report = compare_walks(before, after, prefix_map=[("BMC_9_", "GPU_9_")])
        assert len(_kinds(report)["sensor_removed"]) == 4
        assert report.prefix_paired == 0

    def test_thresholds_are_still_compared_across_the_pairing(self, before):
        """A pairing that skipped the per-pair comparisons would turn the flag into
        a way to hide a real regression behind a declared rename."""
        report = compare_walks(before, _walk("GPU_0_", drop_threshold="TEMP0"),
                               prefix_map=[("HMC_0_", "GPU_0_")])
        assert _kinds(report)["threshold_removed"] == ["GPU_0_TEMP0"]
        assert report.regressions


class TestNameAndPrefixTogetherStillRefuse:
    def test_a_name_change_behind_a_declared_prefix_is_not_paired(self, before):
        """The existing rule holds. Nothing in two walks says which addition
        replaced which removal once both halves of the identity have moved, and a
        wrong guess reads exactly like a right one."""
        report = compare_walks(before, _walk("GPU_0_", rename_one=True),
                               prefix_map=[("HMC_0_", "GPU_0_")])
        kinds = _kinds(report)
        assert kinds["sensor_removed"] == ["HMC_0_TEMP1"]
        assert kinds["sensor_added"] == ["GPU_0_TEMP_1"]
        assert len(kinds["aggregation_prefix_paired"]) == 3

    def test_its_siblings_still_pair(self, before):
        """Refusing the one that moved twice must not cost the three that moved
        once."""
        report = compare_walks(before, _walk("GPU_0_", rename_one=True),
                               prefix_map=[("HMC_0_", "GPU_0_")])
        assert report.prefix_paired == 3


class TestTheMapIsParsedStrictly:
    def test_it_reads_old_equals_new(self):
        assert parse_prefix_map(["A_=B_", "C=D"]) == [("A_", "B_"), ("C", "D")]

    def test_an_empty_new_prefix_means_the_prefix_was_dropped(self):
        assert parse_prefix_map(["HMC_0_="]) == [("HMC_0_", "")]

    @pytest.mark.parametrize("entry", ["nonsense", "=NEW", ""])
    def test_a_malformed_entry_raises_rather_than_being_skipped(self, entry):
        """A typo that silently declared nothing would produce the full
        mass-removal report the flag was passed to prevent, with no sign that the
        flag had not been understood."""
        with pytest.raises(ValueError, match="aggregation-prefix"):
            parse_prefix_map([entry])

    def test_the_longest_prefix_wins(self):
        """Given two entries where one is a prefix of the other, a shortest-first
        pass produces the right answer only by coincidence."""
        report = compare_walks(_walk("HMC_0_"), _walk("GPU_0_"),
                               prefix_map=[("HMC_", "WRONG_"), ("HMC_0_", "GPU_0_")])
        assert report.prefix_paired == 4


class TestTheNameOnlyVariantIsARealFirmwareToo:
    """Some implementations number sensors positionally inside ONE chassis, so the
    prefix reaches the name and the URI never moves.

    Served over real HTTP through the mock, which numbers exactly that way. It is a
    different presentation of the same change and it must not be a regression once
    declared -- and the URI pass reports it as a rename until it is, which IS a
    regression. So the flag has to reach this case too.
    """

    @staticmethod
    def _served(prefix: str):
        bmc = MockBMC(shape="sensors")
        for name, reading in HOST:
            bmc.add(name, reading=reading, upper_critical=100.0)
        for name, reading in SATELLITE:
            bmc.add(f"{prefix}{name}", reading=reading, upper_critical=100.0)
        with serve(bmc) as url:
            return walk_chassis(RedfishClient(url))

    def test_a_stable_uri_makes_the_change_read_as_four_renames(self):
        """The measurement, not a claim about it. This is why the fixture above had
        to put the prefix in the URI: with the URI stable there is no mass removal
        to prevent, and the false regression arrives under a different name."""
        report = compare_walks(self._served("HMC_0_"), self._served("GPU_0_"))
        assert len(_kinds(report)["sensor_renamed"]) == 4
        assert report.regressions

    def test_the_declared_map_pairs_it_too(self):
        report = compare_walks(self._served("HMC_0_"), self._served("GPU_0_"),
                               prefix_map=[("HMC_0_", "GPU_0_")])
        assert report.prefix_paired == 4
        assert "sensor_renamed" not in _kinds(report)
        assert report.regressions == []


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "bmc_sensor_audit.cli", *argv],
                          capture_output=True, text=True,
                          env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})


class TestTheCommandLine:
    @pytest.fixture
    def captures(self, tmp_path, before, after):
        first, second = tmp_path / "before.json", tmp_path / "after.json"
        first.write_text(json.dumps(before.to_dict(), indent=2))
        second.write_text(json.dumps(after.to_dict(), indent=2))
        return str(first), str(second)

    def test_the_undeclared_run_exits_1_and_names_the_prefix(self, captures):
        result = _run("regression", "--before", captures[0], "--after", captures[1])
        assert result.returncode == 1
        assert "--aggregation-prefix HMC_0_=GPU_0_" in result.stdout

    def test_the_declared_run_exits_clean(self, captures):
        result = _run("regression", "--before", captures[0], "--after", captures[1],
                      "--aggregation-prefix", "HMC_0_=GPU_0_")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_the_flag_is_repeatable(self, captures):
        result = _run("regression", "--before", captures[0], "--after", captures[1],
                      "--aggregation-prefix", "NOTHING_=ALSO_NOTHING_",
                      "--aggregation-prefix", "HMC_0_=GPU_0_")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_malformed_flag_exits_2_before_reading_a_walk(self, captures):
        """2, not 1: the run could not be made as asked. Checked before either
        capture is opened, so a mistyped flag is not buried under a report."""
        result = _run("regression", "--before", captures[0], "--after", captures[1],
                      "--aggregation-prefix", "HMC_0_")
        assert result.returncode == 2
        assert "is not OLD=NEW" in result.stderr
        assert "Firmware regression" not in result.stdout

    def test_the_json_shape_separates_the_two_counts(self, captures):
        result = _run("regression", "--before", captures[0], "--after", captures[1],
                      "--aggregation-prefix", "HMC_0_=GPU_0_", "--json")
        payload = json.loads(result.stdout)
        assert payload["paired"] == 6
        assert payload["paired_through_declared_prefix"] == 4

    def test_the_text_report_breaks_the_count_out(self, captures):
        result = _run("regression", "--before", captures[0], "--after", captures[1],
                      "--aggregation-prefix", "HMC_0_=GPU_0_")
        assert "through a declared prefix map" in result.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
