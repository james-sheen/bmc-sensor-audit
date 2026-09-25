"""`adopt` writes the fit's spread beside its gain, and the engine reads both.

The engine proposes a `gain_sigma` with every fitted gain: the standard error of
the fit, which is how well the readings pin the gain down. Until supplemental
format 3 there was nowhere to write one, so an adopted gain went into the file
as though it were exact, and a datasheet tolerance could not be written at all.

**WHAT THE SPREAD IS NOT.** It is not what makes an adopted coupling graded, and
this file measures that rather than saying it: the last class runs the round
trip -- fit, adopt, a resident run, a graded projection -- and the same run with
the spread stripped out grades the same projections. What made them gradeable
was the rollout's seed; the spread adds the gain's own doubt to a band the
driver's forecast already draws.

THE BENCH IS NOISY ON PURPOSE. `test_adopt_writes_a_fitted_gain_down.py` fits a
noise-free series to prove the number is right, and its standard error is
3e-18 -- a spread that says nothing. Here the driven series carries its own
noise, so the fit has a standard error worth writing, and the driver is a level
read through noise so the projector the generator puts on it can be fitted.
**Nothing here is a figure about any real board.**
"""

from __future__ import annotations

import json
import pathlib
import random
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip(
    "arbiter_engine.api",
    reason="arbiter-engine is the optional [detect] extra; Stage 1 does not require it")

from bmc_sensor_audit import adopt as _adopt          # noqa: E402
from bmc_sensor_audit import cli                      # noqa: E402

DRIVER = "FAN_A_TACH"
DRIVEN = "OUTLET_TEMP"
PROPOSAL = f"{DRIVER} -> {DRIVEN}"
CADENCE = 60.0
TRUE_GAIN = 0.004
FIT_WALKS = 200          # the engine fits nothing below 120 paired changes
RESIDENT_CYCLES = 16
PREVIOUS = "presence-audit/supplemental/2"
CARRYING = "presence-audit/supplemental/3"

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
    "Name": "spread fixture baseboard", "Probe": "TRUE", "Type": "Board",
}


def _series(steps):
    """A wandering level read through noise, driving a noisy reading one
    collection step later. Seeded, so every run fits the same number."""
    rng = random.Random(20260925)
    level, tach = 3000.0, []
    for _ in range(steps):
        level += rng.gauss(0.0, 15.0)
        tach.append(level + rng.gauss(0.0, 60.0))
    temp = [30.0 + TRUE_GAIN * (tach[i - 1] if i else tach[0])
            + rng.gauss(0.0, 0.2) for i in range(steps)]
    return tach, temp


def _walk(tach, temp):
    def point(name, reading, units, low, high):
        return {"name": name, "path": f"/redfish/v1/Chassis/C/Sensors/{name}",
                "reading": reading, "units": units, "state": "Enabled",
                "health": "OK", "shape": "sensors",
                "thresholds": {"lower/critical": low, "upper/critical": high}}
    return {"format": "bmc-sensor-audit/walk/1",
            "chassis": ["/redfish/v1/Chassis/C"],
            "shapes_seen": ["sensors"], "errors": [],
            "sensors": [point(DRIVER, tach, "RPM", 500.0, 20000.0),
                        point(DRIVEN, temp, "Cel", 5.0, 85.0)]}


@pytest.fixture(scope="module")
def bench():
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config").mkdir()
    (root / "config" / "board.json").write_text(json.dumps(CONFIG))
    tach, temp = _series(FIT_WALKS + RESIDENT_CYCLES)
    walks = root / "walks"
    walks.mkdir()
    paths = []
    for index in range(FIT_WALKS):
        path = walks / f"walk{index:04d}.json"
        path.write_text(json.dumps(_walk(tach[index], temp[index])))
        paths.append(str(path))
    return {"config": str(root / "config"), "walks": paths,
            "tach": tach, "temp": temp}


def _supplemental_file(fmt=PREVIOUS) -> pathlib.Path:
    path = pathlib.Path(tempfile.mkdtemp()) / "supplemental.json"
    path.write_text(json.dumps({
        "format": fmt, "provenance": "a fixture; states nothing about any board",
        "sampling_interval_s": CADENCE,
        "couplings": [{"from": DRIVER, "to": DRIVEN,
                       "propagation_delay_s": CADENCE,
                       "time_constant_s": CADENCE, "response_model": "step",
                       "gain": "estimate",
                       "basis": "a fixture: the driven series is built from "
                                "the driver one collection step later"}]},
        indent=2))
    return path


def _adopt_cmd(bench, supplemental, *extra):
    argv = ["adopt", "--config", bench["config"],
            "--supplemental", str(supplemental)]
    for walk in bench["walks"]:
        argv += ["--walk", walk]
    return cli.main(argv + list(extra))


def _listed_spread(bench, capsys) -> float:
    assert _adopt_cmd(bench, _supplemental_file(), "--list") == 0
    out = capsys.readouterr().out
    assert "gain_sigma " in out, out
    return float(out.split("gain_sigma ")[1].split(",")[0])


@pytest.fixture(scope="module")
def adopted(bench):
    """One adoption, shared: fitting two hundred walks is the slow half."""
    path = _supplemental_file()
    assert _adopt_cmd(bench, path, "--proposal", PROPOSAL, "--force") == 0
    return path, json.loads(path.read_text())


class TestTheSpreadIsTheOneTheEngineProposed:

    def test_the_listing_shows_it_before_anything_is_written(self, bench, capsys):
        assert _listed_spread(bench, capsys) > 0

    def test_it_is_written_as_listed(self, bench, adopted, capsys):
        _path, document = adopted
        coupling = document["couplings"][0]
        assert coupling["gain_sigma"] == pytest.approx(
            _listed_spread(bench, capsys), rel=1e-5)

    def test_the_interval_it_came_from_holds_the_truth(self, adopted):
        """Not a test of the engine's statistics -- a check that the number
        written is the right kind of number: the gain sits within a few
        standard errors of the value the fixture generated it from."""
        coupling = adopted[1]["couplings"][0]
        assert abs(coupling["gain"] - TRUE_GAIN) < 4 * coupling["gain_sigma"]


class TestItsBasisSaysWhatItAssumes:

    def test_it_is_its_own_sentence(self, adopted):
        coupling = adopted[1]["couplings"][0]
        assert coupling["gain_sigma_basis"] != coupling["gain_basis"]
        assert f"adopted_from_proposal {PROPOSAL} at " in coupling["gain_sigma_basis"]

    def test_it_names_the_estimator_and_its_support(self, adopted):
        basis = adopted[1]["couplings"][0]["gain_sigma_basis"]
        for fragment in ("standard error of the fitted gain", "paired changes",
                         f"{CADENCE:g}s collection grid", "step response",
                         "not how much they scatter"):
            assert fragment in basis, basis

    def test_it_carries_the_engines_verdict_on_the_assumption(self, adopted):
        """A `step` fit is the one where the engine reports the standard error
        readable as stated. The sentence has to say which verdict it got,
        because nothing in the file can recover it afterwards."""
        basis = adopted[1]["couplings"][0]["gain_sigma_basis"]
        assert "readable as stated on a step fit" in basis, basis
        assert "lag-1 residual autocorrelation" in basis, basis

    def test_the_spread_sits_beside_the_number_it_is_about(self, adopted):
        keys = list(adopted[1]["couplings"][0])
        at = keys.index("gain")
        assert keys[at:at + 4] == ["gain", "gain_basis", "gain_sigma",
                                   "gain_sigma_basis"], keys


class TestTheFormatIsRaisedOnlyAsFarAsItMustBe:

    def test_a_format_2_file_is_raised_to_the_format_that_carries_it(
            self, adopted):
        assert adopted[1]["format"] == CARRYING

    def test_and_the_run_says_so(self, bench, capsys):
        path = _supplemental_file()
        assert _adopt_cmd(bench, path, "--proposal", PROPOSAL, "--force") == 0
        assert f"raised from {PREVIOUS!r} to {CARRYING!r}" in capsys.readouterr().out

    def test_a_file_that_already_carries_it_keeps_its_header(self, bench):
        path = _supplemental_file(CARRYING)
        assert _adopt_cmd(bench, path, "--proposal", PROPOSAL, "--force") == 0
        assert json.loads(path.read_text())["format"] == CARRYING

    def test_the_oldest_carrying_format_is_chosen(self):
        assert _adopt.format_for_spread(PREVIOUS) == CARRYING
        assert _adopt.format_for_spread(CARRYING) is None

    def test_a_core_that_carries_none_is_refused_by_name(self, monkeypatch):
        """A core older than format 3 would write the gain, drop the spread,
        and report success. Refused, naming the release that carries it."""
        from presence_audit import supplemental

        monkeypatch.delattr(supplemental, "COUPLING_KEYS_BY_FORMAT")
        with pytest.raises(_adopt.AdoptionRefused, match="0.1.11"):
            _adopt.format_for_spread(PREVIOUS)

    def test_the_raised_file_loads_through_the_format_that_owns_it(self, adopted):
        from presence_audit.supplemental import load_supplemental

        path, document = adopted
        coupling = load_supplemental(str(path)).couplings[0]
        assert coupling.gain_sigma == pytest.approx(
            document["couplings"][0]["gain_sigma"])


class TestWithoutASpreadTheGainIsStillWritten:
    """The engine can propose a standard error of zero, or none at all, and the
    format refuses a zero because the engine reads it as no spread. The gain
    is written either way -- adopting it was the request -- and nothing claims
    a spread. A merely tiny one, like the 3e-18 of a noise-free fit, is still
    that fit's standard error and is written as it is."""

    def _proposal(self, sigma):
        return _adopt.Proposal(
            id=PROPOSAL, driver=DRIVER, driven=DRIVEN, gain=TRUE_GAIN, n=198,
            r_squared=1.0, interval=(TRUE_GAIN, TRUE_GAIN),
            response_model="step", grid_seconds=CADENCE, declared_gain=None,
            replay={}, gain_sigma=sigma)

    @pytest.mark.parametrize("sigma", [None, 0.0, float("nan")])
    def test_no_spread_is_proposed(self, sigma):
        assert self._proposal(sigma).spread is None

    def test_the_gain_goes_in_and_the_header_does_not_move(self):
        path = _supplemental_file()
        written = _adopt.write(path, self._proposal(0.0), "a basis",
                               "a spread basis nobody should see")
        coupling = json.loads(path.read_text())["couplings"][0]
        assert coupling["gain"] == pytest.approx(TRUE_GAIN)
        assert "gain_sigma" not in coupling
        assert "gain_sigma_basis" not in coupling
        assert json.loads(path.read_text())["format"] == PREVIOUS
        assert written.spread is None and written.format_raised_to is None


class TestItIsProvenToHaveLanded:

    def test_a_spread_that_did_not_load_back_restores_the_file(
            self, bench, monkeypatch):
        """Parsing is not the post-condition. A loader that accepted the file
        and dropped the spread would leave a file that reads as adopted with a
        spread and is not; the writer checks, restores and refuses."""
        from presence_audit import supplemental

        real = supplemental.load_supplemental

        def drops_the_spread(path):
            loaded = real(path)
            for coupling in loaded.couplings:
                object.__setattr__(coupling, "gain_sigma", None)
            return loaded

        path = _supplemental_file()
        before = path.read_text()
        monkeypatch.setattr(supplemental, "load_supplemental", drops_the_spread)
        proposal = _adopt.Proposal(
            id=PROPOSAL, driver=DRIVER, driven=DRIVEN, gain=TRUE_GAIN, n=198,
            r_squared=0.5, interval=(0.0036, 0.0048), response_model="step",
            grid_seconds=CADENCE, declared_gain=None, replay={},
            gain_sigma=0.0003)
        with pytest.raises(_adopt.AdoptionRefused, match="spread"):
            _adopt.write(path, proposal, "a basis", "a spread basis")
        assert path.read_text() == before

    def test_dry_run_names_the_spread_and_the_raise_and_writes_nothing(
            self, bench, capsys):
        path = _supplemental_file()
        before = path.read_text()
        assert _adopt_cmd(bench, path, "--proposal", PROPOSAL, "--force",
                          "--dry-run") == 0
        out = capsys.readouterr().out
        assert "would set gain_sigma " in out
        assert "would set gain_sigma_basis" in out
        assert f"would raise the format to {CARRYING!r}" in out
        assert path.read_text() == before


class TestTheEngineReadsItBack:
    """A spread written into a file nothing reads is provenance theatre."""

    def test_the_declared_transition_carries_it(self, bench, adopted):
        import yaml

        from arbiter_engine.api import EngineSession, model_describe
        from presence_audit.generator import generate
        from presence_audit.supplemental import load_supplemental
        from bmc_sensor_audit.inventory.entity_manager import load_declaration

        path, document = adopted
        model, _manifest = generate(load_declaration([bench["config"]]),
                                    domain_id="bmc-sensor-audit",
                                    supplemental=load_supplemental(str(path)))
        model_path = pathlib.Path(tempfile.mkdtemp()) / "model.yaml"
        model_path.write_text(yaml.safe_dump(model))
        session = EngineSession()
        session.load_model(str(model_path))
        declared = model_describe(session).to_dict()["model"]["transitions"][
            "declared"]
        assert [row.get("gain_sigma") for row in declared] == [
            pytest.approx(document["couplings"][0]["gain_sigma"])], declared


class TestTheAdoptedCouplingIsGraded:
    """The round trip, and what the spread does and does not do in it.

    Fit, adopt, then a resident run over the readings that follow: the
    coupling's own projections reach the coupling leg of the calibration.
    The same run with the spread removed grades the same projections -- the
    rollout is seeded from the driver's forecast, whose band the gain passes
    through, so the gain's doubt only widens a band that already exists.
    """

    def _resident(self, bench, path, monkeypatch):
        from arbiter_engine import SqlitePredictionLedger
        from bmc_sensor_audit.inventory.redfish import walk_from_dict

        state = {"i": FIT_WALKS,
                 "at": datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)}

        def walk(_client):
            i = state["i"]
            state["i"] += 1
            return walk_from_dict(_walk(bench["tach"][i], bench["temp"][i]))

        def now():
            at = state["at"]
            state["at"] = at + timedelta(seconds=CADENCE)
            return at

        monkeypatch.setattr(cli, "walk_chassis", walk)
        monkeypatch.setattr(cli, "_client", lambda _args: None)
        monkeypatch.setattr(cli, "_now", now)
        monkeypatch.setattr(cli, "_sleep", lambda _seconds: None)
        ledger = pathlib.Path(tempfile.mkdtemp()) / "ledger.sqlite"
        cli.main(["detect", "--config", bench["config"],
                  "--target", "https://bench.invalid",
                  "--supplemental", str(path), "--resident",
                  "--ledger", str(ledger), "--cycles", str(RESIDENT_CYCLES)])
        store = SqlitePredictionLedger(str(ledger))
        try:
            return store.calibration()["own_projections"]
        finally:
            store.close()

    def _copy(self, adopted, strip):
        document = json.loads(json.dumps(adopted[1]))
        if strip:
            for coupling in document["couplings"]:
                coupling.pop("gain_sigma")
                coupling.pop("gain_sigma_basis")
        path = pathlib.Path(tempfile.mkdtemp()) / "supplemental.json"
        path.write_text(json.dumps(document))
        return path

    def test_its_projections_are_graded(self, bench, adopted, monkeypatch):
        own = self._resident(bench, self._copy(adopted, strip=False),
                             monkeypatch)
        assert own["n"] > 0, own

    def test_the_spread_is_not_what_made_them_gradeable(
            self, bench, adopted, monkeypatch):
        with_spread = self._resident(bench, self._copy(adopted, strip=False),
                                     monkeypatch)
        without = self._resident(bench, self._copy(adopted, strip=True),
                                 monkeypatch)
        assert without["n"] == with_spread["n"] > 0, (with_spread, without)
