"""The problem-solving loop, run in this domain.

Every stage the engine offers either answers or declines by a name the engine
publishes -- never an exception, never silence. This is one half of a two-domain
test; `operating-health-audit` carries the other, on a domain that shares no
noun with this one. The question both halves answer is about the ENGINE: did
running the loop here need anything it does not offer every domain?

THE BOARD is the vendored Ampere Mt. Jade configuration with the supplemental
file this package ships for it -- two flows, the fan-control coupling, and the
fault channel the zone assignment gives. The one thing added for the test is an
action on the fan, because the shipped file claims no override path and the act
stage needs something declared to act with. The walks are synthesized: the
zone's ambient reading over its bound for two cycles, then under it. Nothing
here is a measurement of any board.

THE SESSION is the one `detect --resident` builds: a fresh engine session each
cycle over one shared history and one ledger, fed through the core's feeder.
The case book lives beside that ledger, which is what lets a case opened in one
cycle be resolved by the checks of the next.

The published names are read from where the engine keeps them: its top-level
decline enum and one closed vocabulary per discipline. Both are deeper than the
names the engine promises, and are imported anyway because the test's subject is
exactly that promise -- a decline outside those sets is one no reader could
switch on.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MTJADE = ROOT / "tests" / "fixtures" / "upstream" / "ampere" / "mtjade.json"
BOARD_FILE = ROOT / "examples" / "supplemental" / "ampere-mtjade.json"
FAN, TEMP = "FAN3_1", "TS4_Temp"
START = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
CADENCE = 60.0
#: The ambient reading per cycle: over its bound of 50.0 twice, then under it.
AMBIENT = (55.0, 56.0, 45.0, 44.0)
BREACHED = 2


def _engine():
    """Skipped per test, not per module: a module-level skip would move this
    file out of one of the README's two collected populations and not the
    other, and the README states the gap between them."""
    return pytest.importorskip(
        "arbiter_engine.api", reason="the loop's stages are engine capabilities")


def _published() -> set:
    from arbiter_engine.subenvelope import VOCABULARIES
    from arbiter_engine.types import NotEvaluatedReason

    return {reason.value for reason in NotEvaluatedReason}.union(*VOCABULARIES.values())


#: Where a payload lists what it declined, one row per decline with its reason.
#: `not_fitted` is the learn leg's: a coupling it could not fit, and why.
DECLINE_LISTS = ("not_checked", "declines", "not_fitted")


def _reasons(payload) -> list:
    """Every decline reason anywhere in a payload, however deeply it is mounted."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in DECLINE_LISTS and isinstance(value, list):
                    found.extend(item.get("reason") for item in value
                                 if isinstance(item, dict))
                elif key == "declined" and isinstance(value, list):
                    found.extend(v for v in value if isinstance(v, str))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def _unpublished(payload) -> list:
    published = _published()
    return sorted({r for r in _reasons(payload) if r not in published})


def _walk(ambient: float, step: int):
    from bmc_sensor_audit.inventory.redfish import walk_from_dict

    def point(name, reading, units, thresholds):
        return {"name": name, "path": f"/redfish/v1/Chassis/mtjade/Sensors/{name}",
                "reading": reading, "units": units, "state": "Enabled",
                "health": "OK", "shape": "sensors", "thresholds": thresholds}
    return walk_from_dict({
        "format": "bmc-sensor-audit/walk/1",
        "chassis": ["/redfish/v1/Chassis/mtjade"], "shapes_seen": ["sensors"],
        "errors": [],
        "sensors": [point(TEMP, ambient, "Cel", {"upper/critical": 50.0}),
                    point(FAN, 3000.0 + 7.0 * step, "RPM",
                          {"upper/critical": 23100.0, "lower/critical": 500.0})]})


@pytest.fixture(scope="module")
def loop(tmp_path_factory):
    """Four resident cycles, the case opened after the first, and every stage
    asked of the session the cycle built."""
    api = _engine()
    import yaml
    from arbiter_engine import InMemoryObservationHistory, SqlitePredictionLedger

    from bmc_sensor_audit.inventory.entity_manager import load_declaration
    from presence_audit.diff import compare
    from presence_audit.feeder import feed
    from presence_audit.generator import generate
    from presence_audit.supplemental import load_supplemental

    tmp = tmp_path_factory.mktemp("loop")
    document = json.loads(BOARD_FILE.read_text())
    document["actions"] = [{
        "name": "set_fan3", "point": FAN, "effect": "set",
        "basis": "a fixture: the test sets the fan it names. The shipped board "
                 "file claims no override path, and an act stage needs one."}]
    supplemental_path = tmp / "supplemental.json"
    supplemental_path.write_text(json.dumps(document))

    declaration = load_declaration([str(MTJADE)])
    model, manifest = generate(declaration, domain_id="bmc-sensor-audit",
                               supplemental=load_supplemental(str(supplemental_path)))
    # THE OPERATOR'S DECLARATION of when a case closes: nothing at or above a
    # warning on the case's reading for two checks running. The engine will not
    # choose these numbers, and this package does not ship them -- they are the
    # operator's to state, as a bound on a reading is.
    model["domain"]["cases"] = {"severity": "warning", "consecutive_checks": 2}
    model_path = tmp / "model.yaml"
    model_path.write_text(yaml.safe_dump(model))

    history = InMemoryObservationHistory()
    ledger = SqlitePredictionLedger(str(tmp / "ledger.db"))
    out = {"manifest": manifest, "cycles": [], "case": None}
    for step, ambient in enumerate(AMBIENT):
        at = START + timedelta(seconds=CADENCE * step)
        with api.as_of(at):
            session = api.EngineSession(history=history, ledger=ledger)
            session.load_model(str(model_path))
            fed = feed(session, manifest, [compare(declaration, _walk(ambient, step))])
            envelope = api.check(session).to_dict()
            cycle = {"at": at, "session": session, "fed": fed, "envelope": envelope}
            if step == 0:
                cycle["described"] = api.model_describe(session).to_dict()
                cycle["hypothesis"] = api.hypothesize(session, TEMP).to_dict()
                cycle["plan"] = api.plan(session, horizon_s=CADENCE * 4,
                                         step_s=CADENCE).to_dict()
                cycle["act"] = api.file_action(
                    session, {"template": "set_fan3", "entity_id": FAN,
                              "parameters": {"value": 6000.0}},
                    at, "the loop's own record of a fixture action").to_dict()
                opened = api.open_case(session, TEMP, "reading",
                                       basis="the ambient reading over its bound")
                cycle["opened"] = opened.to_dict()
                case_id = cycle["opened"]["case"].get("case_id")
                out["case"] = case_id
                for stage, envelope_of in (("hypothesize", cycle["hypothesis"]),
                                           ("plan", cycle["plan"]),
                                           ("act", cycle["act"])):
                    api.attach_stage(session, case_id, stage, envelope_of)
            out["cycles"].append(cycle)
    out["book"] = api.case_book(out["cycles"][-1]["session"]).to_dict()
    return out


class TestEveryStageAnswersOrDeclinesByName:

    def test_sense(self, loop):
        first = loop["cycles"][0]["envelope"]
        assert any(f["entity_id"] == TEMP for f in first["findings"]), (
            "the ambient reading over its bound produced no finding")
        for cycle in loop["cycles"]:
            assert _unpublished(cycle["envelope"]) == []

    def test_model(self, loop):
        """The model says, per stage, whether it declares what that stage reads,
        and this board declares every one but an objective to plan against."""
        described = loop["cycles"][0]["described"]
        stages = {name: row["declared"] for name, row in
                  described["model"]["stages"].items()}
        assert stages == {"check": True, "hypothesize": True, "plan": False,
                          "act": True, "learn": True, "case": True}
        assert _unpublished(described) == []

    def test_hypothesize(self, loop):
        """The fault channel names the fan as a cause of the ambient reading and
        states no strength, so the honest answer is the fan with no posterior
        and the reason there is none: `cpt_missing`, on the leg and on the row."""
        payload = loop["cycles"][0]["hypothesis"]
        leg = payload["hypothesis"]
        assert [c["cause"] for c in leg["candidates"]] == [FAN], leg["candidates"]
        assert "cpt_missing" in {row["reason"] for row in leg["not_checked"]}, (
            leg["not_checked"])
        for candidate in leg["candidates"]:
            assert candidate["posterior"] is None, candidate
            assert candidate["declined"] == ["cpt_missing"], candidate
        assert "cpt_missing" in _published()
        assert _unpublished(payload) == []

    def test_plan(self, loop):
        """An action is declared and no objective is, so the plan evaluates and
        ranks nothing, by name."""
        payload = loop["cycles"][0]["plan"]
        reasons = _reasons(payload)
        assert {"no_objective", "no_candidates"} & set(reasons), reasons
        assert _unpublished(payload) == []

    def test_act(self, loop):
        """The fixture's fan action, recorded as executed. The format gives an
        action no tolerance and the coupling runs from the temperature to the
        fan, so there is no band to grade the written value against: the
        execution is recorded and declined by name rather than filed."""
        payload = loop["cycles"][0]["act"]
        leg = payload["execution"]
        assert leg.get("id"), "the execution was refused rather than recorded"
        filed = leg["checked"]["pairs_filed"]
        assert filed or "no_declared_tolerance" in _reasons(payload), leg["checked"]
        assert _unpublished(payload) == []

    def test_learn(self, loop):
        """The coupling is declared with its gain withheld, so the engine may
        propose one. One walk is far too few to fit it, and the leg names the
        edge and the reason rather than going quiet. With nothing fitted the
        writer's dry run has nothing to write, and proposes nothing."""
        from bmc_sensor_audit import adopt

        described = loop["cycles"][0]["described"]
        proposed = described["model"]["proposed_transitions"]
        declined = {row["edge"]: row["reason"] for row in proposed["not_fitted"]}
        assert proposed["fitted"] or declined.get(f"{TEMP}->{FAN}"), proposed
        candidates = adopt.proposals(described, loop["manifest"], grid_seconds=CADENCE)
        if not proposed["fitted"]:
            assert candidates == [], candidates
        for proposal in candidates:
            try:
                adopt.check(proposal, force=False)
            except adopt.AdoptionRefused:
                pass
        assert _unpublished(proposed) == []


class TestTheCaseIsOpenedRunAndResolved:

    def _case(self, loop):
        rows = loop["book"]["cases"]["cases"]
        return next(row for row in rows if row["case_id"] == loop["case"])

    def test_it_opened_on_the_reading(self, loop):
        opened = loop["cycles"][0]["opened"]
        assert opened["case"]["entity_id"] == TEMP
        assert _unpublished(opened) == []

    def test_it_resolved_after_two_clean_checks(self, loop):
        case = self._case(loop)
        outcomes = [entry["outcome"] for entry in case["stages"]["check"]]
        assert outcomes == ["found"] * (BREACHED - 1) + ["clean", "clean"], outcomes
        assert case["status"] == "resolved"

    def test_every_stage_that_ran_is_attached_with_its_declines(self, loop):
        case = self._case(loop)
        for stage in ("hypothesize", "plan", "act"):
            entries = case["stages"][stage]
            assert len(entries) == 1, (stage, entries)
            assert set(entries[0].get("declined", ())) <= _published()
        assert "cpt_missing" in case["stages"]["hypothesize"][0]["declined"]
        assert case["stages"]["learn"] == []

    def test_the_book_counts_what_it_holds(self, loop):
        book = loop["book"]["cases"]
        assert (book["opened"], book["resolved"], book["open"]) == (1, 1, 0)
        assert _unpublished(loop["book"]) == []


def test_the_vocabularies_are_not_empty():
    """Before believing a negative, prove the probe can produce one."""
    _engine()
    assert "insufficient_samples" in _published() and "no_objective" in _published()
    for where in DECLINE_LISTS:
        assert _unpublished({where: [{"reason": "made_up_here"}]}) == ["made_up_here"]
    assert _unpublished({"declined": ["made_up_here"]}) == ["made_up_here"]
