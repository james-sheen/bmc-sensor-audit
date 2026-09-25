"""`detect` files forecasts, and a resident run grades them without a second command.

**MEASURED BEFORE THIS: forty walks of a coupled bench board through `detect
--ledger` left `predictions: 0`.** The ledger was handed to the session and
nothing ever wrote to it, because `detect` called `check` and `model_describe`
and neither files a prediction. The test that closed the wiring filed its one
row by hand, which is how the gap stayed invisible: a ledger that outlives its
process was proven, and a ledger that anything fills was not.

A SHARED LEDGER ALONE CANNOT GRADE EITHER. A forecast is scored against the
reading nearest the moment it matures, inside a grace window. One walk holds one
reading, stamped at its own start -- after that window for every forecast an
earlier run filed -- so every record would mature `ungradeable`. The readings
have to persist too, which is `--history`.

THE BENCH. A live target served by a stand-in walker, and a clock driven by the
test rather than slept through: sixteen collection steps of a minute each in
well under a second. The board is two readings and one declared coupling, the
same shape `adopt`'s bench fits. **Nothing here is a calibration figure about
any real board** -- that needs a horizon to elapse against telemetry, and the
number belongs in a run's own output, not in a test.
"""

from __future__ import annotations

import json
import pathlib
import random
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from bmc_sensor_audit import cli
from bmc_sensor_audit.inventory.redfish import walk_from_dict

DRIVER = "FAN_A_TACH"
DRIVEN = "OUTLET_TEMP"
CADENCE = 60.0
TRUE_GAIN = 0.004
START = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)

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
    "Name": "resident fixture baseboard", "Probe": "TRUE", "Type": "Board",
}


def _engine():
    """Skipped per test, not per module: a module-level skip would move this
    file out of one of the README's two collected populations and not the
    other, and the README states the gap between them."""
    return pytest.importorskip(
        "arbiter_engine.api", reason="filing and grading are engine capabilities")


def _supplemental(tmp, gain="estimate"):
    coupling = {"from": DRIVER, "to": DRIVEN, "propagation_delay_s": CADENCE,
                "time_constant_s": CADENCE, "response_model": "step",
                "gain": gain,
                "basis": "a fixture: the driven series is built from the driver "
                         "one collection step later"}
    if gain != "estimate":
        coupling["gain_basis"] = "the fixture's own generating gain"
    path = tmp / "supplemental.json"
    path.write_text(json.dumps({
        "format": "presence-audit/supplemental/2",
        "provenance": "a fixture; states nothing about any board",
        "sampling_interval_s": CADENCE, "couplings": [coupling]}))
    return path


def _config(tmp):
    (tmp / "config").mkdir()
    (tmp / "config" / "board.json").write_text(json.dumps(CONFIG))
    return tmp / "config"


def _series(steps):
    """A wandering level read through noise, from a seeded generator.

    THE SHAPE THE DRIVER'S PROJECTOR MODELS, and the reason it is not the
    sawtooth `adopt`'s bench uses. The generator puts a local-level projector
    on a coupling's driver, and a local level is identifiable only where the
    first differences are NEGATIVELY autocorrelated -- the signature of
    measurement noise. A deterministic sawtooth is a pattern, not a level plus
    noise, and on it the projector declined `unidentifiable_parameter` every
    cycle: sixteen cycles filed nothing, correctly. Seeded, so the series is
    the same on every run.
    """
    rng = random.Random(20260925)
    level, tach = 3000.0, []
    for _ in range(steps):
        level += rng.gauss(0.0, 15.0)
        tach.append(level + rng.gauss(0.0, 60.0))
    temp = [30.0 + TRUE_GAIN * (tach[i - 1] if i else tach[0]) for i in range(steps)]
    return tach, temp


def _walk(tach, temp):
    def point(name, reading, units, low, high):
        return {"name": name, "path": f"/redfish/v1/Chassis/C/Sensors/{name}",
                "reading": reading, "units": units, "state": "Enabled",
                "health": "OK", "shape": "sensors",
                "thresholds": {"lower/critical": low, "upper/critical": high}}
    return walk_from_dict({
        "format": "bmc-sensor-audit/walk/1", "chassis": ["/redfish/v1/Chassis/C"],
        "shapes_seen": ["sensors"], "errors": [],
        "sensors": [point(DRIVER, tach, "RPM", 500.0, 20000.0),
                    point(DRIVEN, temp, "Cel", 5.0, 85.0)]})


class _Board:
    """A live target whose readings move one step per walk."""

    def __init__(self, steps):
        self.tach, self.temp = _series(steps)
        self.walked = 0

    def walk(self, _client):
        i = self.walked
        self.walked += 1
        return _walk(self.tach[i], self.temp[i])


@pytest.fixture
def bench(monkeypatch):
    """A board, a driven clock, and no network. `_sleep` is a no-op and
    `_now` advances a collection step per call, so a resident run walks through
    simulated time exactly as it would through real time."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    board = _Board(steps=40)
    clock = {"at": START}

    def now():
        at = clock["at"]
        clock["at"] = at + timedelta(seconds=CADENCE)
        return at

    monkeypatch.setattr(cli, "walk_chassis", board.walk)
    monkeypatch.setattr(cli, "_client", lambda _args: None)
    monkeypatch.setattr(cli, "_now", now)
    monkeypatch.setattr(cli, "_sleep", lambda _seconds: None)
    return {"tmp": tmp, "board": board, "config": _config(tmp)}


def _argv(bench, *extra, supplemental=None):
    return ["detect", "--config", str(bench["config"]),
            "--target", "https://bench.invalid",
            "--supplemental", str(supplemental or _supplemental(bench["tmp"])),
            *extra]


def _calibration(path):
    from arbiter_engine import SqlitePredictionLedger

    ledger = SqlitePredictionLedger(str(path))
    try:
        return ledger.calibration()
    finally:
        ledger.close()


class TestAResidentRunGradesItsOwnForecasts:
    """E2's exit criterion, on a bench: a matured record graded inside ONE
    invocation, with no human re-running anything."""

    CYCLES = 16

    def test_a_matured_forecast_is_graded_without_a_second_command(self, bench):
        _engine()
        ledger = bench["tmp"] / "ledger.sqlite"
        code = cli.main(_argv(bench, "--resident", "--ledger", str(ledger),
                              "--cycles", str(self.CYCLES)))
        assert code in (cli.EXIT_CLEAN, cli.EXIT_REGRESSION), code
        cal = _calibration(ledger)
        graded = cal["confirmed"] + cal["falsified"]
        assert graded >= 1, (
            f"{self.CYCLES} cycles filed {cal['recorded']} record(s) and graded "
            f"none; the loop files and never grades, or grades nothing it filed")

    def test_it_walked_once_per_cycle(self, bench):
        _engine()
        cli.main(_argv(bench, "--resident", "--ledger",
                       str(bench["tmp"] / "l.sqlite"), "--cycles", "5"))
        assert bench["board"].walked == 5

    def test_each_cycle_reports_the_ledger_with_its_denominators(self, bench, capsys):
        """Before anything matures the figure is NONE, said as such -- a zero
        would read as a measurement."""
        _engine()
        cli.main(_argv(bench, "--resident", "--ledger",
                       str(bench["tmp"] / "l.sqlite"), "--cycles", str(self.CYCLES)))
        lines = [l for l in capsys.readouterr().out.splitlines()
                 if l.startswith("cycle ")]
        assert len(lines) == self.CYCLES
        assert "confirm_rate none yet" in lines[0]
        assert any("confirm_rate none yet" not in l for l in lines[-3:]), (
            "no cycle ever reported a rate, so nothing matured in the run")

    def test_the_forecasts_are_the_engines_and_the_yardstick_is_beside_them(
            self, bench):
        """`project` files the driver's declared projector AND a random walk on
        the same series, so the figure is a comparison and not a lone number."""
        _engine()
        ledger = bench["tmp"] / "ledger.sqlite"
        cli.main(_argv(bench, "--resident", "--ledger", str(ledger),
                       "--cycles", str(self.CYCLES)))
        by_model = _calibration(ledger)["by_model"]
        assert any(m.startswith("local_level") for m in by_model), by_model
        assert any(m.startswith("baseline") for m in by_model), by_model


def _coupled_run(bench, capsys, cycles, **supplemental):
    _engine()
    ledger = bench["tmp"] / "ledger.sqlite"
    path = _supplemental(bench["tmp"], **supplemental)
    cli.main(_argv(bench, "--resident", "--ledger", str(ledger),
                   "--cycles", str(cycles), supplemental=path))
    out = capsys.readouterr()
    return _calibration(ledger), out.out, out.err


class TestAWrittenGainIsGraded:
    """A coupling's own projection reaches the ledger once its gain is written.

    UNTIL 0.3.5 IT NEVER DID, AND THE REASON WAS MISREAD. The rollout that files
    it was seeded from the CURRENT readings with no action scheduled, which
    moves nothing: every value is held at its reading, so nothing any coupling
    predicts is in it. The engine declined all of it `no_declared_tolerance`,
    naming a declared spread as the remedy, and this class pinned that as the
    limitation -- written or withheld, nothing graded. A declared spread files
    nothing on such a rollout either; that was measured too. Seeded from the
    driver's forecast, the driver moves, the coupling carries the move
    downstream, and the forecast's own band passes through the gain.
    """

    CYCLES = 16

    def test_it_reaches_the_coupling_leg_of_the_calibration(self, bench, capsys):
        cal, _out, err = _coupled_run(bench, capsys, self.CYCLES,
                                      gain=TRUE_GAIN)
        assert cal["own_projections"]["n"] > 0, (cal["own_projections"], err)
        assert "coupling filed nothing" not in err, err

    def test_its_yardstick_is_filed_beside_it(self, bench, capsys):
        """A coupling's figure alone cannot say whether the gain carries
        information; the engine races a random walk beside each projection."""
        cal, _out, _err = _coupled_run(bench, capsys, self.CYCLES,
                                       gain=TRUE_GAIN)
        assert cal["own_projections"]["baseline"]["n"] > 0, cal["own_projections"]

    def test_the_cycle_line_reports_it_with_its_count(self, bench, capsys):
        _cal, out, _err = _coupled_run(bench, capsys, self.CYCLES,
                                       gain=TRUE_GAIN)
        lines = [l for l in out.splitlines() if l.startswith("cycle ")]
        assert any("coupling projections crps" in l for l in lines), lines[-1]
        assert any("coupling value(s)" in l for l in lines), lines[-1]


class TestAWithheldGainSaysWhyItFiledNothing:
    """With the gain withheld the coupling projects nothing, which is the
    format's rule; the run names the engine's reason, and silence would read
    as a coupling being graded."""

    CYCLES = 8

    def test_it_names_the_withheld_gain(self, bench, capsys):
        cal, _out, err = _coupled_run(bench, capsys, self.CYCLES)
        assert cal["own_projections"]["n"] == 0
        assert "coupling filed nothing -- gain_not_adopted" in err, err

    def test_it_is_said_once_not_every_cycle(self, bench, capsys):
        _cal, _out, err = _coupled_run(bench, capsys, self.CYCLES)
        assert err.count("gain_not_adopted") == 1, err


class TestSeparateRunsNeedTheReadingsToo:
    """The cron shape: one walk per invocation, the stores shared on disk."""

    RUNS = 12

    def _run(self, bench, *stores):
        from arbiter_engine.api import as_of

        for step in range(self.RUNS):
            with as_of(START + timedelta(seconds=CADENCE * step)):
                cli.main(_argv(bench, *stores))

    def test_with_history_a_later_run_grades_an_earlier_one(self, bench):
        _engine()
        ledger, history = bench["tmp"] / "l.sqlite", bench["tmp"] / "h.sqlite"
        self._run(bench, "--ledger", str(ledger), "--history", str(history))
        cal = _calibration(ledger)
        assert cal["confirmed"] + cal["falsified"] >= 1, cal

    def test_without_history_nothing_can_even_be_fitted(self, bench, capsys):
        """One walk is one reading and a forecast needs five, so separate runs
        with no durable readings file NOTHING -- and each run says why."""
        _engine()
        ledger = bench["tmp"] / "l.sqlite"
        self._run(bench, "--ledger", str(ledger))
        assert _calibration(ledger)["recorded"] == 0
        assert "--history PATH" in capsys.readouterr().err


class TestACombinationThatWouldDoNothingIsRefused:

    @pytest.mark.parametrize("extra", [
        ("--resident", "--ledger", "l.sqlite"),        # no --target
    ])
    def test_resident_needs_a_live_target(self, bench, extra):
        argv = ["detect", "--config", str(bench["config"]),
                "--walk", "w.json", *extra]
        assert cli.main(argv) == cli.EXIT_INCOMPLETE

    def test_resident_needs_a_ledger(self, bench):
        assert cli.main(_argv(bench, "--resident")) == cli.EXIT_INCOMPLETE

    def test_history_is_refused_for_a_recorded_walk(self, bench, capsys):
        argv = ["detect", "--config", str(bench["config"]),
                "--walk", "w.json", "--history", "h.sqlite"]
        assert cli.main(argv) == cli.EXIT_INCOMPLETE
        assert "invented times" in capsys.readouterr().err

    def test_cadence_flags_without_resident_are_refused(self, bench):
        assert cli.main(_argv(bench, "--cycles", "3")) == cli.EXIT_INCOMPLETE


class TestTheOldGapStaysClosed:

    def test_a_single_live_run_with_history_files_once_it_can_fit(self, bench):
        """Direct evidence for the defect: the rows are counted in the file,
        not read back through the engine that wrote them."""
        _engine()
        ledger, history = bench["tmp"] / "l.sqlite", bench["tmp"] / "h.sqlite"
        TestSeparateRunsNeedTheReadingsToo()._run(
            bench, "--ledger", str(ledger), "--history", str(history))
        rows = sqlite3.connect(str(ledger)).execute(
            "select count(*) from predictions").fetchone()[0]
        assert rows > 0, "the ledger file holds no rows"
