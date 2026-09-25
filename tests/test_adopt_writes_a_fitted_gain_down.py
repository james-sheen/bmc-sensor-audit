"""`adopt` is the first thing in this family that writes into a model.

The engine fits a coupling's gain and refuses to write it anywhere; the ruling
recorded beside `_proposed_transitions` says a VERTICAL may -- in its own file,
under a command a person invoked, recording a basis. This is the round trip that
ruling permits: fit, gate, write, and read the number back through the engine.

**IT WOULD HAVE WRITTEN A WRONG NUMBER UNTIL TODAY,** which is the reason this
file starts by fitting a known truth rather than by checking the writer. Two
defects sat between a declared coupling and a usable gain, both in
`presence-audit`, and both invisible from either side of the seam:

  * nothing ever added the RELATIONSHIP, so a declared coupling produced
    `couplings_seen: 0` and a run with one was byte-identical to a run without;
  * and the feeder stamped every observation sixty seconds apart whatever
    `sampling_interval_s` said, so a delay validated against the collector's
    grid was applied on a different one. Measured on a driven series generated
    from its driver at exactly one interval, with the file declaring 300 s:
    **-0.0023 against a truth of +0.0040** -- wrong sign, r-squared 0.33, and
    an interval excluding the true value.

That is the number `adopt` would have written into somebody's file with a
sentence beside it saying it was measured. So the first class here fits a
series whose gain is known and asserts the engine recovers it AT THE DECLARED
CADENCE -- the check that fails if either defect returns.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

pytest.importorskip(
    "arbiter_engine.api",
    reason="arbiter-engine is the optional [detect] extra; Stage 1 does not require it")

# PyYAML is imported where it is USED, not at module scope. A module-level
# `importorskip` here would make this file a SECOND module whose presence
# depends on PyYAML, and the README states that `test_action.py` is the whole
# difference between the with-PyYAML and dependency-free populations -- a claim
# about WHICH tests, which a count cannot check and which this file would have
# falsified while both counts still agreed with their own totals.

from bmc_sensor_audit import adopt as _adopt          # noqa: E402
from bmc_sensor_audit.cli import main                 # noqa: E402

DRIVER = "FAN_A_TACH"
DRIVEN = "OUTLET_TEMP"
PROPOSAL = f"{DRIVER} -> {DRIVEN}"
TRUE_GAIN = 0.004
CADENCE = 300.0          # NOT 60: the grid the old feeder assumed
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
    "Name": "adopt fixture baseboard", "Probe": "TRUE", "Type": "Board",
}


def _supplemental(gain="estimate", gain_basis=None):
    coupling = {"from": DRIVER, "to": DRIVEN,
                "propagation_delay_s": CADENCE,
                "time_constant_s": CADENCE,
                "response_model": "step",
                "gain": gain,
                "basis": ("a fixture: the driven series was generated from the "
                          "driver at exactly one collection interval")}
    if gain_basis is not None:
        coupling["gain_basis"] = gain_basis
    return {"format": "presence-audit/supplemental/2",
            "provenance": "a fixture; states nothing about any board",
            "sampling_interval_s": CADENCE,
            "couplings": [coupling]}


def _series():
    """A driver that moves, and a driven series built from it at the true lag.

    Deterministic and RNG-free: a sawtooth whose period does not divide the lag,
    so the delay is identifiable rather than aliased onto a neighbouring one.
    """
    tach = [3000.0 + 700.0 * ((i * 7) % 11) / 10.0 for i in range(WALKS)]
    temp = [30.0 + TRUE_GAIN * (tach[i - 1] if i else tach[0])
            for i in range(WALKS)]
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
    """One generated board and its walks, shared: writing two hundred walk
    files is the slow half and none of it depends on the test."""
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config").mkdir()
    (root / "config" / "board.json").write_text(json.dumps(CONFIG))
    walks = root / "walks"
    walks.mkdir()
    tach, temp = _series()
    paths = []
    for index in range(WALKS):
        path = walks / f"walk{index:04d}.json"
        path.write_text(json.dumps(_walk(tach[index], temp[index])))
        paths.append(str(path))
    return {"config": str(root / "config"), "walks": paths, "root": root}


def _supplemental_file(bench, **kwargs) -> pathlib.Path:
    path = pathlib.Path(tempfile.mkdtemp()) / "supplemental.json"
    path.write_text(json.dumps(_supplemental(**kwargs), indent=2))
    return path


def _run(bench, supplemental, *extra):
    argv = ["adopt", "--config", bench["config"], "--supplemental",
            str(supplemental)]
    for walk in bench["walks"]:
        argv += ["--walk", walk]
    return main(argv + list(extra))


class TestTheFitRecoversAKnownTruthAtTheDeclaredCadence:
    """The premise, and the regression guard for both `presence-audit` defects.
    Everything below measures nothing if the number being adopted is wrong."""

    def test_the_proposal_is_listed_under_the_operators_own_names(
            self, bench, capsys):
        assert _run(bench, _supplemental_file(bench), "--list") == 0
        out = capsys.readouterr().out
        assert PROPOSAL in out, out

    def test_the_grid_it_was_fitted_on_is_stated(self, bench, capsys):
        """A fitted gain is a statement about the spacing it was fitted at, and
        the spacing used to be sixty seconds whatever the file said."""
        _run(bench, _supplemental_file(bench), "--list")
        assert f"{CADENCE:g}s collection grid" in capsys.readouterr().out

    def test_the_fitted_gain_is_the_true_one(self, bench, capsys):
        _run(bench, _supplemental_file(bench), "--list")
        out = capsys.readouterr().out
        gain = float(out.split("gain ")[1].split()[0])
        assert gain == pytest.approx(TRUE_GAIN, rel=1e-3), (
            f"fitted {gain} against a truth of {TRUE_GAIN}; the coupling is "
            f"being fitted on the wrong grid or between the wrong pair")


class TestTheReplayGateRefusesRatherThanScoringZero:

    def test_without_a_corpus_it_refuses(self, bench, capsys):
        path = _supplemental_file(bench)
        assert _run(bench, path, "--proposal", PROPOSAL) == 2
        assert "no corpus" in capsys.readouterr().err

    def test_and_the_file_is_untouched(self, bench):
        path = _supplemental_file(bench)
        before = path.read_text()
        _run(bench, path, "--proposal", PROPOSAL)
        assert path.read_text() == before

    def test_force_adopts_it_and_says_it_was_untested(self, bench):
        path = _supplemental_file(bench)
        assert _run(bench, path, "--proposal", PROPOSAL, "--force") == 0
        coupling = json.loads(path.read_text())["couplings"][0]
        assert coupling["gain"] == pytest.approx(TRUE_GAIN, rel=1e-3)
        assert _adopt.ADOPTED_UNTESTED in coupling["gain_basis"]

    def test_the_two_stamps_are_different_names(self):
        """A proposal nobody could test and one that was tested and found
        useless are opposite facts with different remedies -- file a corpus,
        or do not adopt. One name for both tells a later reader the wrong one."""
        assert _adopt.ADOPTED_UNTESTED != _adopt.ADOPTED_WITHOUT_REPLAY_GAIN


class TestTheGateArms:
    """The arms, on a constructed proposal rather than a constructed corpus.

    A corpus where adopting the gain genuinely detects MORE is a fixture about
    the engine's scoring, not about this gate, and building one would make this
    file fail whenever the engine's axioms moved. What is under test here is
    which of four facts produces which of four outcomes.
    """

    def _proposal(self, **replay):
        return _adopt.Proposal(
            id=PROPOSAL, driver=DRIVER, driven=DRIVEN, gain=TRUE_GAIN,
            n=198, r_squared=1.0, interval=(0.0039, 0.0041),
            response_model="step", grid_seconds=CADENCE,
            declared_gain=None, replay=replay)

    def test_a_replay_that_gained_needs_no_stamp(self):
        assert _adopt.check(
            self._proposal(status="replayed", delta=2, detected_before=3,
                           detected_after=5, confirmed=6),
            force=False) is None

    def test_a_replay_that_gained_nothing_is_refused(self):
        with pytest.raises(_adopt.AdoptionRefused) as raised:
            _adopt.check(self._proposal(status="replayed", delta=0,
                                        detected_before=3, detected_after=3,
                                        confirmed=6), force=False)
        assert "--force" in str(raised.value)

    def test_and_forcing_it_stamps_which_one_it_was(self):
        assert _adopt.check(
            self._proposal(status="replayed", delta=0, detected_before=3,
                           detected_after=3, confirmed=6),
            force=True) == _adopt.ADOPTED_WITHOUT_REPLAY_GAIN

    def test_a_replay_that_LOST_ground_is_refused_too(self):
        """`delta <= 0`, not `delta == 0`. A proposal that detects FEWER is the
        one case where adopting is actively worse, and a strict-zero test would
        have let it through."""
        with pytest.raises(_adopt.AdoptionRefused):
            _adopt.check(self._proposal(status="replayed", delta=-1,
                                        detected_before=3, detected_after=2,
                                        confirmed=6), force=False)

    def test_a_declared_gain_is_never_overwritten_even_with_force(self):
        """The half of the ruling that does not move. `gain: estimate` is an
        author asking for a number; a declared one is their claim about the
        machine, and replacing it from a fit is the act the engine is forbidden
        to do, one process further away."""
        declared = _adopt.Proposal(
            id=PROPOSAL, driver=DRIVER, driven=DRIVEN, gain=TRUE_GAIN, n=198,
            r_squared=1.0, interval=(0.0039, 0.0041), response_model="step",
            grid_seconds=CADENCE, declared_gain=0.01,
            replay={"status": "replayed", "delta": 5, "detected_before": 0,
                    "detected_after": 5, "confirmed": 6})
        for force in (False, True):
            with pytest.raises(_adopt.AdoptionRefused) as raised:
                _adopt.check(declared, force=force)
            assert "FINDING" in str(raised.value)


class TestWhatGoesIntoTheFile:

    @pytest.fixture
    def adopted(self, bench):
        path = _supplemental_file(bench)
        assert _run(bench, path, "--proposal", PROPOSAL, "--force") == 0
        return path, json.loads(path.read_text())["couplings"][0]

    def test_the_basis_names_the_proposal_and_when(self, adopted):
        _path, coupling = adopted
        assert f"adopted_from_proposal {PROPOSAL} at " in coupling["gain_basis"]

    def test_it_carries_the_support_the_decision_rested_on(self, adopted):
        """None of this is recoverable from the file afterwards, and all of it
        changes whether a reviewer would have adopted the number."""
        _path, coupling = adopted
        for fragment in ("r_squared", "paired changes", "interval",
                         f"{CADENCE:g}s collection grid", "step response"):
            assert fragment in coupling["gain_basis"], coupling["gain_basis"]

    def test_the_basis_sits_beside_the_number_it_explains(self, adopted):
        """Key order. A provenance sentence three keys away from the gain is
        one a reviewer reads separately from it."""
        _path, coupling = adopted
        keys = list(coupling)
        assert keys.index("gain_basis") == keys.index("gain") + 1

    def test_the_author_s_own_basis_survives(self, adopted):
        """`basis` says why the coupling EXISTS and `gain_basis` says where the
        number came from. Adopting answers the second question and must not
        overwrite the answer to the first."""
        _path, coupling = adopted
        assert "generated from the driver" in coupling["basis"]

    def test_the_file_still_loads_through_the_format_that_owns_it(self, adopted):
        from presence_audit.supplemental import load_supplemental

        path, _coupling = adopted
        loaded = load_supplemental(str(path))
        assert not loaded.couplings[0].gain_is_withheld
        assert loaded.couplings[0].gain == pytest.approx(TRUE_GAIN, rel=1e-3)

    def test_a_coupling_that_states_no_gain_key_at_all_is_still_written(
            self, bench):
        """`gain:` is OPTIONAL in this format -- absent means withheld, exactly
        as `estimate` does. Rebuilding the block by walking its existing keys
        wrote nothing at all for such a coupling and reported success, which is
        the one outcome every refusal in this command exists to prevent: a file
        that reads as adopted and is not."""
        import json as _json
        path = _supplemental_file(bench)
        document = _json.loads(path.read_text())
        document["couplings"][0].pop("gain")
        path.write_text(_json.dumps(document, indent=2))

        assert _run(bench, path, "--proposal", PROPOSAL, "--force") == 0
        coupling = _json.loads(path.read_text())["couplings"][0]
        assert coupling["gain"] == pytest.approx(TRUE_GAIN, rel=1e-3)
        assert "adopted_from_proposal" in coupling["gain_basis"]

    def test_dry_run_writes_nothing(self, bench, capsys):
        path = _supplemental_file(bench)
        before = path.read_text()
        assert _run(bench, path, "--proposal", PROPOSAL, "--force",
                    "--dry-run") == 0
        assert path.read_text() == before
        assert "unchanged" in capsys.readouterr().out


class TestTheEngineThenSeesTheNumber:
    """The far end of the round trip. A gain written into a file nothing reads
    back is provenance theatre."""

    @pytest.fixture
    def described(self, bench):
        path = _supplemental_file(bench)
        assert _run(bench, path, "--proposal", PROPOSAL, "--force") == 0

        import yaml

        from arbiter_engine.api import EngineSession, model_describe
        from presence_audit.diff import compare
        from presence_audit.feeder import feed
        from presence_audit.generator import generate
        from presence_audit.supplemental import load_supplemental
        from bmc_sensor_audit.inventory.entity_manager import load_declaration
        from bmc_sensor_audit.inventory.redfish import walk_from_dict

        declaration = load_declaration([bench["config"]])
        supplemental = load_supplemental(str(path))
        model, manifest = generate(declaration, domain_id="bmc-sensor-audit",
                                   supplemental=supplemental)
        model_path = pathlib.Path(tempfile.mkdtemp()) / "model.yaml"
        model_path.write_text(yaml.safe_dump(model))
        session = EngineSession()
        session.load_model(str(model_path))
        reports = [compare(declaration,
                           walk_from_dict(json.loads(
                               pathlib.Path(w).read_text())))
                   for w in bench["walks"]]
        feed(session, manifest, reports)
        return model, model_describe(session).to_dict()

    def test_the_model_now_declares_the_number(self, described):
        model, _payload = described
        rule = model["domain"]["relationship_rules"][0]
        assert rule["transition"]["gain"] == pytest.approx(TRUE_GAIN, rel=1e-3)

    def test_and_it_carries_the_provenance_into_the_model(self, described):
        """`gain_basis` becomes the transition's `source`. An adopted number
        that arrived in the model anonymous would be indistinguishable from one
        somebody read off a datasheet."""
        model, _payload = described
        source = model["domain"]["relationship_rules"][0]["transition"]["source"]
        assert "adopted_from_proposal" in source

    def test_the_coupling_no_longer_reports_as_unadopted(self, described):
        _model, payload = described
        transitions = payload["model"]["transitions"]
        assert "gain_not_adopted" not in json.dumps(transitions), transitions

    def test_the_fit_is_now_a_check_on_the_declaration(self, described):
        """The loop closes: with a number declared, the same fit that proposed
        it becomes a DISAGREEMENT test against it -- and agrees with itself."""
        _model, payload = described
        proposed = payload["model"]["proposed_transitions"]
        assert proposed["checked"]["couplings_seen"] == 1
        assert proposed["disagreements"] == [], proposed["disagreements"]
        assert proposed["fitted"][0]["declared_gain"] == pytest.approx(
            TRUE_GAIN, rel=1e-3)
