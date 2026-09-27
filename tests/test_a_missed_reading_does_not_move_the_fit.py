"""One missed reading used to reverse the gain `adopt` writes down.

The core feeds each sensor's history as a ladder: one reading per collection
slot, ending at the engine's clock. A ladder cannot hold a gap, so when a
sensor failed to read in one walk, the core closed the gap up. Every earlier
reading of THAT sensor then sat one slot later than its walk, while its coupled
partner's did not. The fit paired the driver with the wrong walk of the driven.

MEASURED on this package's own adopt fixture: a driven series generated from
its driver at exactly one interval, 200 walks, true gain 0.004. Complete, the
fit recovered 0.004. With the fan tachometer missing ONE reading at walk 191,
it proposed -0.0021 with an interval of [-0.0026, -0.0016], and `--force`
wrote that number into the file. A miss at walk 11 gave 0.00366, with the truth
outside its interval.

`presence-audit` 0.2.3 feeds a coupled sensor from its last missed reading on,
and says so. So a miss early in the run leaves a fit on the walks after it,
which recovers the truth. A late miss leaves too few paired changes, so nothing
is fitted. Both outcomes are true statements, and this file holds both. Walks
here carry no capture time, because the feeder never reads one. That is the
next thing the core owes, and its README says why it waits.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

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
    "Name": "missed-reading fixture baseboard", "Probe": "TRUE", "Type": "Board",
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
    """The adopt fixture's series: a sawtooth whose period does not divide the
    lag, and a driven reading built from the driver one walk earlier."""
    tach = [3000.0 + 700.0 * ((i * 7) % 11) / 10.0 for i in range(WALKS)]
    temp = [30.0 + TRUE_GAIN * (tach[i - 1] if i else tach[0]) for i in range(WALKS)]
    return tach, temp


def _point(name, reading, units, low, high):
    return {"name": name, "path": f"/redfish/v1/Chassis/C/Sensors/{name}",
            "reading": reading, "units": units, "state": "Enabled",
            "health": "OK", "shape": "sensors",
            "thresholds": {"lower/critical": low, "upper/critical": high}}


def _bench(*, driver_missing=(), driven_missing=()):
    """Walk files, and a supplemental file that `adopt` may write into.
    A missed reading is an enabled sensor reporting no value, which is how a
    BMC reports one."""
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config").mkdir()
    (root / "config" / "board.json").write_text(json.dumps(CONFIG))
    supplemental = root / "supplemental.json"
    supplemental.write_text(json.dumps(SUPPLEMENTAL, indent=2))
    tach, temp = _series()
    argv = ["adopt", "--config", str(root / "config"),
            "--supplemental", str(supplemental)]
    for index in range(WALKS):
        walk = {"format": "bmc-sensor-audit/walk/1",
                "chassis": ["/redfish/v1/Chassis/C"],
                "shapes_seen": ["sensors"], "errors": [],
                "sensors": [
                    _point(DRIVER, None if index + 1 in driver_missing else tach[index],
                           "RPM", 500.0, 20000.0),
                    _point(DRIVEN, None if index + 1 in driven_missing else temp[index],
                           "Cel", 5.0, 85.0)]}
        path = root / f"walk{index:04d}.json"
        path.write_text(json.dumps(walk))
        argv += ["--walk", str(path)]
    return argv, supplemental


def _gain(out):
    assert PROPOSAL in out, out
    return float(out.split("gain ")[1].split()[0])


class TestAMissEarlyInTheRunStillFitsTheTruth:

    def test_the_fit_is_the_true_gain(self, capsys):
        argv, _ = _bench(driver_missing={11})
        assert main(argv + ["--list"]) == 0
        gain = _gain(capsys.readouterr().out)
        assert gain == pytest.approx(TRUE_GAIN, rel=1e-3), (
            f"fitted {gain} against a truth of {TRUE_GAIN}: the readings before "
            f"the missed one are being paired with the wrong walk")

    def test_and_it_says_what_the_fit_rests_on(self, capsys):
        argv, _ = _bench(driver_missing={11})
        main(argv + ["--list"])
        out = capsys.readouterr().out
        assert f"{DRIVER}: 189 of 199 readings; it missed walk 11 of {WALKS}" in out


class TestAMissLateInTheRunWritesNoNumber:
    """Too few paired changes remain after the miss, so nothing is fitted -- and
    the number 0.2.2 fitted here had the wrong sign."""

    @pytest.mark.parametrize("missing", [{"driver_missing": {191}},
                                         {"driven_missing": {191}}],
                             ids=["driver", "driven"])
    def test_nothing_is_proposed_and_the_reason_is_printed(self, missing, capsys):
        argv, _ = _bench(**missing)
        assert main(argv + ["--list"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("Nothing fitted."), out
        assert f"it missed walk 191 of {WALKS}" in out

    def test_force_has_nothing_to_write(self):
        argv, supplemental = _bench(driver_missing={191})
        before = supplemental.read_text()
        assert main(argv + ["--proposal", PROPOSAL, "--force"]) == 2
        assert supplemental.read_text() == before
