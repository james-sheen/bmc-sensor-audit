"""`--by-capture-time`: recorded walks placed by the times they carry, when asked.

Until `presence-audit` 0.2.4 the core placed every walk of a recorded run on the
declared grid by its order, ending at the engine's clock, and never read
`captured_at`. A walk nobody took closed up; a sensor that missed one reading
had to be cut at the miss, because a ladder cannot hold a gap. On this
package's adopt fixture (200 walks, true gain 0.004) a miss at walk 191 then
left too few paired changes, and nothing was fitted.

With `--by-capture-time` each walk takes the grid slot its time falls in,
counted back from the newest, and the run is judged as of the newest walk.
MEASURED through `adopt --list` on the same fixture, stamped: every
missed-reading case fits 0.004 on 197 pairs, and twenty whole walks missing fit
0.004 on 177, where the grid gave 0.00395.

IT IS ASKED FOR, NEVER INFERRED FROM THE STAMPS. The first build placed stamped
walks by time on its own, and pairing it against its consumers found a harness
that captures walks seconds apart: they shared slots and the run was refused.
Measured on twelve stamped walks of a frozen fan, declared cadence 60 s: placed
by time, walks 2 s apart are refused and walks an hour apart pass, because a
window counts declared slots; placed in order, the fan is found both times.
Without the flag every walk is placed as before, and this file holds that too.

The judging instant is not a detail either. Placed by time and judged at the
wall clock, every reading falls outside the window, STABILITY declines
`insufficient_samples`, and the frozen fan exits 0.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip(
    "arbiter_engine.api",
    reason="arbiter-engine is the optional [detect] extra; Stage 1 does not require it")

from bmc_sensor_audit.cli import main                 # noqa: E402

DRIVER = "FAN_A_TACH"
DRIVEN = "OUTLET_TEMP"
PROPOSAL = f"{DRIVER} -> {DRIVEN}"
TRUE_GAIN = 0.004
CADENCE = 300.0
WALKS = 200
START = datetime(2026, 9, 1, tzinfo=timezone.utc)

#: The adopt fixture's board and supplemental file, as the missed-reading test
#: declares them: a driven reading generated from its driver one walk earlier.
CONFIG = {
    "Exposes": [
        {"Index": 0, "Name": DRIVER, "Type": "I2CFan",
         "Thresholds": [{"Direction": "greater than", "Name": "upper critical",
                         "Severity": 1, "Value": 20000.0},
                        {"Direction": "less than", "Name": "lower critical",
                         "Severity": 1, "Value": 500.0}]},
        {"Index": 1, "Name": DRIVEN, "Type": "TMP421",
         "Thresholds": [{"Direction": "greater than", "Name": "upper critical",
                         "Severity": 1, "Value": 85.0},
                        {"Direction": "less than", "Name": "lower critical",
                         "Severity": 1, "Value": 5.0}]},
    ],
    "Name": "placed-by-time fixture baseboard", "Probe": "TRUE", "Type": "Board",
}
SUPPLEMENTAL = {
    "format": "presence-audit/supplemental/2",
    "provenance": "a fixture; states nothing about any board",
    "sampling_interval_s": CADENCE,
    "couplings": [{"from": DRIVER, "to": DRIVEN, "propagation_delay_s": CADENCE,
                   "time_constant_s": CADENCE, "response_model": "step",
                   "gain": "estimate",
                   "basis": ("a fixture: the driven series was generated from "
                             "the driver at exactly one collection interval")}],
}


def _series():
    tach = [3000.0 + 700.0 * ((i * 7) % 11) / 10.0 for i in range(WALKS)]
    temp = [30.0 + TRUE_GAIN * (tach[i - 1] if i else tach[0]) for i in range(WALKS)]
    return tach, temp


def _point(name, reading, units, low, high):
    return {"name": name, "path": f"/redfish/v1/Chassis/C/Sensors/{name}",
            "reading": reading, "units": units, "state": "Enabled",
            "health": "OK", "shape": "sensors",
            "thresholds": {"lower/critical": low, "upper/critical": high}}


def _bench(command, *, walks=WALKS, cadence=CADENCE, driver_missing=(),
           driven_missing=(), walks_missing=(), shift=None, frozen=False,
           by_time=True, stamped=True):
    """Walk files stamped one `cadence` apart, and the argv to read them.

    A missed reading is an enabled sensor reporting no value, which is how a
    BMC reports one; a missed walk is a file that was never written. `shift`
    moves one walk's stamp by that many seconds, as a collector's jitter does.
    """
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config").mkdir()
    (root / "config" / "board.json").write_text(json.dumps(CONFIG))
    supplemental = root / "supplemental.json"
    supplemental.write_text(json.dumps(SUPPLEMENTAL, indent=2))
    tach, temp = _series()
    argv = [command, "--config", str(root / "config")]
    if command == "adopt":
        argv += ["--supplemental", str(supplemental)]
    if by_time:
        argv.append("--by-capture-time")
    for index in range(walks):
        if index + 1 in walks_missing:
            continue
        driver = 3000.0 if frozen else tach[index]
        when = START + timedelta(seconds=index * cadence
                                 + ((shift or {}).get(index + 1, 0.0)))
        walk = {"format": "bmc-sensor-audit/walk/1",
                "chassis": ["/redfish/v1/Chassis/C"],
                "shapes_seen": ["sensors"], "errors": [],
                **({"captured_at": when.isoformat(timespec="seconds")}
                   if stamped else {}),
                "sensors": [
                    _point(DRIVER, None if index + 1 in driver_missing else driver,
                           "RPM", 500.0, 20000.0),
                    _point(DRIVEN, None if index + 1 in driven_missing else temp[index],
                           "Cel", 5.0, 85.0)]}
        path = root / f"walk{index:04d}.json"
        path.write_text(json.dumps(walk))
        argv += ["--walk", str(path)]
    return argv, supplemental


def _fit(out):
    assert PROPOSAL in out, out
    gain = float(out.split("gain ")[1].split()[0])
    pairs = int(out.split(" n ")[1].split()[0])
    return gain, pairs


class TestAMissedReadingIsAnEmptySlot:
    """0.2.3 cut a coupled sensor at its last miss; placed by time, nothing is cut."""

    @pytest.mark.parametrize("missed", [
        {"driver_missing": {191}}, {"driven_missing": {191}},
        {"driver_missing": {101}}, {"driver_missing": {11}}],
        ids=["driver-191", "driven-191", "driver-101", "driver-11"])
    def test_the_fit_keeps_every_other_pair(self, missed, capsys):
        argv, _ = _bench("adopt", **missed)
        assert main(argv + ["--list"]) == 0
        out = capsys.readouterr().out
        assert _fit(out) == (pytest.approx(TRUE_GAIN), 197), out
        assert "Fed only from the last missed reading on" not in out


class TestAWalkNobodyTookIsAnEmptySlot:

    def test_twenty_missing_walks_fit_the_truth_and_are_counted(self, capsys):
        argv, _ = _bench("adopt", walks_missing=set(range(101, 121)))
        assert main(argv + ["--list"]) == 0
        out = capsys.readouterr().out
        assert _fit(out) == (pytest.approx(TRUE_GAIN), 177), out
        assert "180 walks in 200 slots of 300s, 20 empty" in out, out


class TestAStampOffTheGridTakesItsSlot:

    def test_jitter_is_snapped_and_the_largest_snap_is_printed(self, capsys):
        argv, _ = _bench("adopt", driver_missing={191},
                         shift={50: 40.0, 120: -35.0, 199: 12.0})
        assert main(argv + ["--list"]) == 0
        out = capsys.readouterr().out
        assert _fit(out) == (pytest.approx(TRUE_GAIN), 197), out
        assert "the largest snap to a slot was 40s" in out, out


class TestTheRunIsJudgedAsOfItsNewestWalk:
    """Readings sit where they were taken, weeks before the clock that reads them."""

    def test_a_frozen_fan_is_found(self, capsys):
        argv, _ = _bench("detect", walks=12, cadence=60.0, frozen=True)
        code = main(argv)
        out = capsys.readouterr().out
        assert "Placed by capture time -- 12 capture(s) in 12 slot(s) of 60 s" in out
        assert code == 1, out
        assert "Findings -- 1" in out, out


class TestWithoutTheFlagNothingChanges:
    """Stamped walks, placed in order, exactly as 0.3.9 placed them."""

    def test_a_late_miss_still_fits_nothing(self, capsys):
        argv, _ = _bench("adopt", driver_missing={191}, by_time=False)
        assert main(argv + ["--list"]) == 0
        out = capsys.readouterr().out
        assert "Nothing fitted" in out and "Placed by" not in out, out
        assert "FAN_A_TACH: 9 of 199 readings; it missed walk 191 of 200" in out, out

    @pytest.mark.parametrize("apart", [2.0, 3600.0], ids=["seconds", "an-hour"])
    def test_a_frozen_fan_is_found_however_far_apart_its_walks_were(self, apart,
                                                                    capsys):
        """The harness case the first build broke, and the one it let pass."""
        argv, _ = _bench("detect", walks=12, cadence=apart, frozen=True,
                         by_time=False)
        code = main(argv)
        out = capsys.readouterr().out
        assert code == 1 and "Findings -- 1" in out, out


class TestWalksThatCannotBePlacedStopTheRun:

    def test_two_walks_in_one_slot_are_refused_and_nothing_is_written(self, capsys):
        argv, supplemental = _bench("adopt", shift={3: -290.0})
        before = supplemental.read_text()
        assert main(argv + ["--proposal", PROPOSAL]) == 2
        err = capsys.readouterr().err
        assert "cannot place these walks" in err and "fall in one slot" in err, err
        assert supplemental.read_text() == before

    def test_walks_with_no_time_are_refused_when_their_times_are_asked_for(
            self, capsys):
        argv, _ = _bench("adopt", walks=20, stamped=False)
        assert main(argv + ["--list"]) == 2
        assert "carry no captured_at" in capsys.readouterr().err

    def test_the_flag_on_a_live_target_is_refused(self, capsys):
        argv, _ = _bench("detect", walks=0)
        argv += ["--target", "https://bmc.invalid"]
        assert main(argv) == 2
        assert "this run walks a live target" in capsys.readouterr().err


class TestWalksTakenFurtherApartThanTheCadenceAreSaidToBe:

    def test_the_note_names_the_empty_slots(self, capsys):
        argv, _ = _bench("detect", walks=12, cadence=3600.0, frozen=True)
        main(argv)
        err = capsys.readouterr().err
        assert ("649 of 661 slots are empty: these walks were taken further apart "
                "than the declared 60s") in err, err
