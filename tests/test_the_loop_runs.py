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
    from arbiter_engine.subenvelope import PUBLISHED_REASONS

    return set(PUBLISHED_REASONS)


# THE WALK IS THE ENGINE'S. This file and operating-health-audit's each carried a
# copy of it, and the copies drifted: this one read four lists, the other two, so a
# learn-stage refusal under `not_fitted` passed the other's check unread. Engine
# 0.2.15 publishes the walker beside the names it checks against, and both loops
# call it -- a list the engine starts reporting declines under is read here the
# day it ships, without a copy to update.
def _reasons(payload) -> list:
    """Every decline reason anywhere in a payload, however deeply it is mounted."""
    from arbiter_engine.subenvelope import declined_reasons

    return declined_reasons(payload)


def _unpublished(payload) -> list:
    from arbiter_engine.subenvelope import unpublished_reasons

    return unpublished_reasons(payload)


def _walk(ambient: float, step: int, supply=None, fan=None):
    """The zone's two points, and PSU0's input and output power when `supply`
    gives them as `(input, output)` in watts. `fan` overrides the fan's reading."""
    from bmc_sensor_audit.inventory.redfish import walk_from_dict

    def point(name, reading, units, thresholds):
        return {"name": name, "path": f"/redfish/v1/Chassis/mtjade/Sensors/{name}",
                "reading": reading, "units": units, "state": "Enabled",
                "health": "OK", "shape": "sensors", "thresholds": thresholds}
    sensors = [point(TEMP, ambient, "Cel", {"upper/critical": 50.0}),
               point(FAN, 3000.0 + 7.0 * step if fan is None else fan, "RPM",
                     {"upper/critical": 23100.0, "lower/critical": 500.0})]
    if supply is not None:
        sensors += [point("PSU0_PINPUT", supply[0], "W", {}),
                    point("PSU0_POUTPUT", supply[1], "W", {})]
    return walk_from_dict({
        "format": "bmc-sensor-audit/walk/1",
        "chassis": ["/redfish/v1/Chassis/mtjade"], "shapes_seen": ["sensors"],
        "errors": [], "sensors": sensors})


def _board(tmp):
    """The board's model and manifest, with the fixture's fan action and the
    operator's case criterion, written where a session can load it."""
    import yaml

    from bmc_sensor_audit.inventory.entity_manager import load_declaration
    from presence_audit.generator import generate
    from presence_audit.supplemental import load_supplemental

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
    return declaration, model_path, manifest


@pytest.fixture(scope="module")
def loop(tmp_path_factory):
    """Four resident cycles, the case opened after the first, and every stage
    asked of the session the cycle built."""
    api = _engine()
    from arbiter_engine import InMemoryObservationHistory, SqlitePredictionLedger

    from presence_audit.diff import compare
    from presence_audit.feeder import feed

    tmp = tmp_path_factory.mktemp("loop")
    declaration, model_path, manifest = _board(tmp)

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
            cycle = {"at": at, "session": session, "fed": fed, "envelope": envelope,
                     # Where the walk up from the zone stood this cycle.
                     "walk": api.hypothesize(session, TEMP).to_dict()[
                         "hypothesis"].get("walk")}
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
                cycle["gaps"] = api.gaps(session).to_dict()
                for stage, envelope_of in (("hypothesize", cycle["hypothesis"]),
                                           ("plan", cycle["plan"]),
                                           ("act", cycle["act"]),
                                           ("gaps", cycle["gaps"])):
                    api.attach_stage(session, case_id, stage, envelope_of)
            out["cycles"].append(cycle)
    # A PERSON CONFIRMS THE CAUSE, after the case has closed: the fan, which is
    # what the ranking named. The book reads it back against that ranking.
    last = out["cycles"][-1]
    with api.as_of(last["at"] + timedelta(seconds=CADENCE)):
        out["confirmed"] = api.attach_stage(
            last["session"], out["case"], "confirm",
            reference={"cause": FAN, "reading": f"{FAN}.reading",
                       "basis": "the loop's own record of a fixture fault"}).to_dict()
    out["book"] = api.case_book(last["session"]).to_dict()
    return out


@pytest.fixture(scope="module", params=[(500.0, 300.0), (500.0, 480.0)],
                ids=["imbalanced", "balanced"])
def supplies(request, tmp_path_factory):
    """The loop's four cycles with PSU0's input and output power reported, and
    `gaps` asked of the last. The board file declares that power balance, and
    no loss margin, because the supply's efficiency is on its datasheet."""
    api = _engine()
    from arbiter_engine import InMemoryObservationHistory

    from presence_audit.diff import compare
    from presence_audit.feeder import feed

    declaration, model_path, manifest = _board(tmp_path_factory.mktemp("supplies"))
    history = InMemoryObservationHistory()
    for step, ambient in enumerate(AMBIENT):
        at = START + timedelta(seconds=CADENCE * step)
        with api.as_of(at):
            session = api.EngineSession(history=history)
            session.load_model(str(model_path))
            feed(session, manifest,
                 [compare(declaration, _walk(ambient, step, supply=request.param))])
            api.check(session)
            residuals = api.gaps(session).to_dict()["residuals"]
    return request.param, residuals


@pytest.fixture(scope="module")
def sixteen(tmp_path_factory):
    """Sixteen resident cycles with the zone over its bound throughout and the fan
    inside its own: by the last, every check on the fan has its samples, so the
    walk up from the zone screens the fan, and `gaps` is asked of that cycle."""
    api = _engine()
    from arbiter_engine import InMemoryObservationHistory

    from presence_audit.diff import compare
    from presence_audit.feeder import feed

    declaration, model_path, manifest = _board(tmp_path_factory.mktemp("sixteen"))
    history = InMemoryObservationHistory()
    for step in range(16):
        at = START + timedelta(seconds=CADENCE * step)
        with api.as_of(at):
            session = api.EngineSession(history=history)
            session.load_model(str(model_path))
            feed(session, manifest, [compare(declaration, _walk(60.0, step))])
            api.check(session)
            walk = api.hypothesize(session, TEMP).to_dict()["hypothesis"]["walk"]
            residuals = api.gaps(session).to_dict()["residuals"]
    return walk, residuals


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
        # Read by lookup, as the engine's compatibility policy asks of every
        # reader: a patch release may add a stage, and one not named here means
        # the engine is newer than this test, not that the board changed.
        named = {"check": True, "hypothesize": True, "plan": False,
                 "act": True, "learn": True, "case": True,
                 "gaps": True, "confirm": True}
        assert {name: stages.get(name) for name in named} == named
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

    def test_the_fan_is_open_every_cycle_and_never_the_frontier(self, loop):
        """Its reading stays inside its bounds while its other checks lack
        samples, so on every cycle the walk is open on the fan and the fault
        stops nowhere: the fan is never named the place it stops."""
        walks = [cycle["walk"] for cycle in loop["cycles"]]
        assert [walk["state"] for walk in walks] == ["open"] * len(AMBIENT)
        assert all(walk["frontier"] == [] for walk in walks)
        assert all([entry["entity"] for entry in walk["open"]] == [FAN] for walk in walks)

    def test_the_ranking_names_the_fan_as_the_reading_it_rests_on(self, loop):
        """One candidate names itself. The fault channel is the only declared
        path, and it carries no delay, so the fan is read at the finding's
        instant and nothing says `read_at`. Forcing the fan answers nothing
        without a strength, so the row carries null rather than a number."""
        leg = loop["cycles"][0]["hypothesis"]["hypothesis"]
        assert leg["most_discriminating"] == {
            "entity": FAN, "reading": f"{FAN}.reading", "basis": "only_candidate"}
        assert "read_at" not in leg
        [candidate] = leg["candidates"]
        assert candidate["path"] == [f"{FAN}->{TEMP}"]
        assert candidate["do_would_answer"] is None

    def test_gaps(self, loop):
        """No reading the loop takes breaks the board's declared balance, and the
        board declares no relation an entity must have, and no count of actions
        that makes a pattern -- so nothing is located, and the arms it cannot
        read say so by name."""
        residuals = loop["cycles"][0]["gaps"]["residuals"]
        assert residuals["hypotheses"] == []
        assert {(d["reason"], d["location"]) for d in residuals["not_checked"]} == {
            ("missing_config", "presence"), ("missing_config", "gaps.min_cycles")}
        assert _unpublished(residuals) == []

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
        for stage in ("hypothesize", "plan", "act", "gaps", "confirm"):
            entries = case["stages"][stage]
            assert len(entries) == 1, (stage, entries)
            assert set(entries[0].get("declined", ())) <= _published()
        assert "cpt_missing" in case["stages"]["hypothesize"][0]["declined"]
        assert case["stages"]["learn"] == []

    def test_the_confirmed_cause_was_the_one_ranked_first(self, loop):
        """The book reads the confirmation against the ranking attached before it:
        the fan stood first of one, and the reading it named settled it. Counts,
        never a rate."""
        confirmed = loop["book"]["cases"]["confirmed"]
        # BY LOOKUP. A row is a record the engine may add keys to in a patch
        # release -- 0.2.20 added three -- and an equality on the whole row
        # broke on the first one.
        [row] = confirmed["rows"]
        assert (row["case_id"], row["cause"], row["rank"], row["of"],
                row["named_reading_settled_it"]) == (loop["case"], FAN, 1, 1, True)
        assert (confirmed["confirmations"], confirmed["ranked_first"],
                confirmed["not_ranked"]) == (1, 1, 0)
        assert _unpublished(loop["confirmed"]) == []

    def test_the_book_says_that_first_place_was_not_a_posteriors(self, loop):
        """No strength is declared on this board, so the fan stood first of one by
        its standing on the walk, and was named because it was the only
        candidate. Engine 0.2.23 says so on the row, and counts first places a
        posterior decided apart: here, none. A hit rate read off `ranked_first`
        alone would count this."""
        confirmed = loop["book"]["cases"]["confirmed"]
        [row] = confirmed["rows"]
        assert (row["ranked_by"], row["named_by"]) == ("standing", "only_candidate")
        assert row["settling_entity_was_named"] is True
        assert (confirmed["ranked_first"], confirmed["ranked_first_by_posterior"],
                confirmed["ranked_by_posterior"]) == (1, 0, 0)

    def test_the_book_counts_what_it_holds(self, loop):
        book = loop["book"]["cases"]
        assert (book["opened"], book["resolved"], book["open"]) == (1, 1, 0)
        assert _unpublished(loop["book"]) == []


@pytest.fixture(scope="module")
def screened(tmp_path_factory):
    """Sixteen resident cycles with the zone over its bound throughout and the fan
    inside its own, a case opened at the first and the ranking attached at each,
    then the fan confirmed: by the last cycle the walk read the fan sound and
    screened it."""
    api = _engine()
    from arbiter_engine import InMemoryObservationHistory, SqlitePredictionLedger

    from presence_audit.diff import compare
    from presence_audit.feeder import feed

    tmp = tmp_path_factory.mktemp("screened")
    declaration, model_path, manifest = _board(tmp)
    history = InMemoryObservationHistory()
    ledger = SqlitePredictionLedger(str(tmp / "ledger.db"))
    case_id = None
    for step in range(16):
        at = START + timedelta(seconds=CADENCE * step)
        with api.as_of(at):
            session = api.EngineSession(history=history, ledger=ledger)
            session.load_model(str(model_path))
            feed(session, manifest, [compare(declaration, _walk(60.0, step))])
            api.check(session)
            if case_id is None:
                case_id = api.open_case(
                    session, TEMP, "reading",
                    basis="the ambient reading over its bound").to_dict()["case"]["case_id"]
            api.attach_stage(session, case_id, "hypothesize",
                             api.hypothesize(session, TEMP))
    with api.as_of(at + timedelta(seconds=CADENCE)):
        api.attach_stage(session, case_id, "confirm", reference={
            "cause": FAN, "reading": f"{FAN}.reading",
            "basis": "the loop's own record of a fixture fault"})
        return api.case_book(session).to_dict()["cases"]


class TestAConfirmationSaysWhereTheFanStood:
    """The book reads a confirmation back against the walk the case kept, not only
    against the rank (engine 0.2.31). The fan is the board's only declared cause,
    so it is ranked first of one whatever the walk said of it."""

    def test_confirmed_after_four_cycles_the_fan_was_open(self, loop):
        """The case kept one ranking, from the first cycle, when no check on the
        fan had its samples yet."""
        confirmed = loop["book"]["cases"]["confirmed"]
        [row] = confirmed["rows"]
        assert (row["standing"], row["walk_state"], row["walks_before"]) == (
            "open", "open", 1)
        assert row["basis"] == "the loop's own record of a fixture fault"
        assert confirmed["confirmed_after_screened"] == 0

    def test_after_sixteen_the_walk_had_screened_the_fan(self, screened):
        """The walk's own surprise: the fan read sound, and a person confirmed it.
        `ranked_first` still counts it; the new total says the walk screened it."""
        confirmed = screened["confirmed"]
        [row] = confirmed["rows"]
        assert (row["standing"], row["walk_state"], row["walks_before"]) == (
            "screened", "unexplained", 16)
        assert confirmed["confirmed_after_screened"] == 1
        assert (row["rank"], row["of"], confirmed["ranked_first"]) == (1, 1, 1)
        assert _unpublished(screened) == []

    def test_the_confirmation_line_says_where_it_stood(self, screened):
        from bmc_sensor_audit.cli import _confirmation_line

        [row] = screened["confirmed"]["rows"]
        assert (f"{FAN} ranked 1 of 1 (by its standing on the walk, not by "
                f"posterior), screened on an unexplained walk;") in _confirmation_line(row)


class TestTheFirstRungDown:
    """Where the visible fault stops, what it explains below it (engine 0.2.32)."""

    def test_a_stalled_fan_explains_the_hot_zone_and_its_action_applies(self, tmp_path):
        """A fixture fault: the fan under its lower bound, the zone over its upper
        one. The walk is traced to the fan, which explains the zone's finding; the
        loop's fixture action on the fan applies to it."""
        api = _engine()
        from presence_audit.diff import compare
        from presence_audit.feeder import feed

        declaration, model_path, manifest = _board(tmp_path)
        with api.as_of(START):
            session = api.EngineSession()
            session.load_model(str(model_path))
            feed(session, manifest, [compare(declaration, _walk(60.0, 0, fan=100.0))])
            api.check(session)
            walk = api.hypothesize(session, TEMP).to_dict()["hypothesis"]["walk"]
        assert walk["state"] == "traced"
        [row] = walk["frontier"]
        assert (row["entity"], [r["entity"] for r in row["explains"]],
                row["findings_explained"], row["actions"]) == (FAN, [TEMP], 1, ["set_fan3"])

    def test_a_fan_inside_its_bounds_is_on_no_frontier(self, loop):
        """Every cycle of the loop: the fan sound or still short of samples, so
        no walk has a rung to give."""
        assert all(not cycle["walk"]["frontier"] for cycle in loop["cycles"])


class TestGapsSaysWhereTheWalkEnds:

    def test_a_finding_every_declared_cause_screens_is_located(self, sixteen):
        """The zone over its bound and the fan, its only declared cause, reading
        sound: the finding is unexplained, which on this board is the true
        answer, and `gaps` says so (engine 0.2.30). `drives` joins the zone to
        the fan, but the fan shows nothing and the two are joined causally
        already, so no undeclared channel is a candidate."""
        walk, residuals = sixteen
        assert walk["state"] == "unexplained"
        [row] = [h for h in residuals["hypotheses"]
                 if h["kind"] == "unexplained_finding"]
        assert (row["at"], row["screened"], row["candidates"]) == (TEMP, [FAN], [])
        assert "no relation without a causal direction" in row["reason"]
        assert residuals["checked"]["walk_states"]["unexplained"] == 1
        assert _unpublished(residuals) == []


class TestAPowerBalanceThatDoesNotCloseIsLocated:
    """The board file declares that PSU0's output power balances its input, from
    the PMBus labels of one device. `gaps` reads the check's finding back to that
    declaration and says where the power goes missing."""

    def test_an_imbalance_is_located_between_the_supplys_two_readings(self, supplies):
        (supplied, delivered), residuals = supplies
        located = residuals["hypotheses"]
        if delivered / supplied > 0.9:
            assert located == [], located
            return
        [row] = located
        assert (row["kind"], row["at"], row["basis"]) == (
            "unaccounted_flow", "PSU0_PINPUT", "CONSERVATION")
        assert row["between"] == ["reading", "peer_PSU0_POUTPUT"]
        assert row["ratio"] == pytest.approx((supplied - delivered) / supplied)
        # Both declared readings were taken, so the loss leaves by a path the
        # board file does not declare, and no reading is named to settle it.
        assert row["evidence_needed"] is None and row["reason"]
        assert _unpublished(residuals) == []


def test_the_vocabularies_are_not_empty():
    """Before believing a negative, prove the probe can produce one."""
    _engine()
    assert "insufficient_samples" in _published() and "no_objective" in _published()
    from arbiter_engine.subenvelope import DECLINE_KEYS

    for where in DECLINE_KEYS:
        assert _unpublished({where: [{"reason": "made_up_here"}]}) == ["made_up_here"]
    assert _unpublished({"declined": ["made_up_here"]}) == ["made_up_here"]
