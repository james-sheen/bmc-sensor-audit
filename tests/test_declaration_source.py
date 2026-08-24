"""Declaration sources that are not entity-manager: `pdr/1` and `fleet-baseline/1`.

On NVIDIA-managed platforms the GPU and HMC sensors arrive as runtime
self-description — PLDM PDRs and NSM discovery, projected to Redfish — and have no
entity-manager entry. They land in coverage's reverse direction, *machine has,
declaration doesn't*, and nothing about them can ever be a regression because nothing
ever expected them.

Three rules carry the whole feature, and each has its own class below:

**Precedence is pinned.** The manufacturer's declaration wins wherever it declares;
`pdr/1` covers what it does not; `fleet-baseline/1` is the explicit last resort.

**A candidate refuses to be consumed.** The tool will emit a `pdr/1` from a walk. What
it emits asserts nothing, and the loader refuses it until a person adds their name.
The circularity hazard is the founding problem of this tool one door over: a
declaration derived from a walk of an unprovisioned board is an empty declaration that
reads healthy, and nothing inside the file can tell that from a good one.

**Every run that used one says so.** Provenance in text and in JSON, and a
`fleet-baseline/1` says in words that it is a downgrade.

**The fourth rule has no heading and is the one that would have shipped silently.**
`diff` excludes declarations whose entity-manager `Type` does not produce a reading.
These sources have no entity-manager Type at all, so every entry would classify
`UNRECOGNISED`, be counted, be printed, and never once fail a gate — the exact vacuous
pass this tool exists to catch, arriving through the feature built to prevent it.
`TestAbsenceFromAnAlternateSourceIsARegression` is that check.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bmc_sensor_audit.inventory.declaration_source import (  # noqa: E402
    FLEET_BASELINE_FORMAT, PDR_FORMAT, SOURCE_PRECEDENCE, DeclarationSourceError,
    candidate_from_walk, load_declaration_source, merge_sources)
from bmc_sensor_audit.inventory.diff import compare  # noqa: E402
from bmc_sensor_audit.inventory.entity_manager import (  # noqa: E402
    parse_config_text)
from bmc_sensor_audit.inventory.redfish import (  # noqa: E402
    RedfishClient, walk_chassis)
from bmc_sensor_audit.testing.mock_redfish import MockBMC, serve  # noqa: E402

# The host board, declared by the manufacturer the ordinary way.
BOARD = {"Name": "Board", "Exposes": [
    # The threshold matches what the mock BMC serves. A deliberate mismatch here
    # would put a `threshold_drift` regression into every run below, and each test
    # would then be asserting an exit code it got for the wrong reason.
    {"Name": "INLET_TEMP", "Type": "TMP75",
     "Thresholds": [{"Name": "upper critical", "Direction": "greater than",
                     "Severity": 1, "Value": 95.0}]},
    {"Name": "SHARED_TEMP", "Type": "TMP75"},
]}


def _pdr(*, sensors=("GPU0_TEMP", "GPU1_TEMP"), **overrides) -> dict:
    """A reviewed `pdr/1`. Tests wanting a candidate mutate `reviewed` themselves,
    because absent, null and half-filled are three different files and a boolean
    parameter cannot express the difference."""
    payload = {
        "format": PDR_FORMAT,
        "platform": "HGX-H100",
        "firmware": "1.03.05",
        "captured_at": "2026-08-24T09:00:00+00:00",
        "reviewed": {"by": "an operator", "on": "2026-08-24"},
        "sensors": [{"name": name, "thresholds": {"upper/critical": 95.0}}
                    for name in sensors],
    }
    payload.update(overrides)
    return payload


def _candidate(**overrides) -> dict:
    """What the emitter writes: the slot present and explicitly null."""
    return _pdr(reviewed=None, **overrides)


def _fleet(*, sensors=("GPU0_TEMP",), **overrides) -> dict:
    payload = {
        "format": FLEET_BASELINE_FORMAT,
        "platform": "HGX-H100",
        "derived_from": "412 units at 3 firmware levels",
        "reviewed": {"by": "an operator", "on": "2026-08-24"},
        "sensors": [{"name": name} for name in sensors],
    }
    payload.update(overrides)
    return payload


def _write(tmp_path: Path, name: str, payload: dict) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload, indent=2))
    return str(path)


def _load(tmp_path: Path, payload: dict, name: str = "source.json"):
    return load_declaration_source(_write(tmp_path, name, payload))


def _walk(*names: str):
    bmc = MockBMC(shape="sensors")
    for name in names:
        bmc.add(name, reading=41.0, upper_critical=95.0)
    with serve(bmc) as url:
        return walk_chassis(RedfishClient(url))


class TestPrecedenceIsPinned:
    def test_the_order_is_manufacturer_then_pdr_then_fleet(self):
        """Read off the constant rather than restated, so the ruling has one home."""
        assert SOURCE_PRECEDENCE == ("entity-manager", PDR_FORMAT,
                                     FLEET_BASELINE_FORMAT)

    def test_the_manufacturer_wins_wherever_it_declares(self, tmp_path):
        declaration = parse_config_text(json.dumps(BOARD), "board.json")
        source = _load(tmp_path, _pdr(sensors=("SHARED_TEMP", "GPU0_TEMP")))
        merged = merge_sources(declaration, [source])

        shared = [s for s in merged.sensors if s.name == "SHARED_TEMP"]
        assert len(shared) == 1, "the sensor is declared twice, and one of the two " \
                                 "would be expected and permanently absent"
        assert shared[0].source == "board.json"
        assert merged.sources[0].supplied == ("GPU0_TEMP",)

    def test_pdr_outranks_fleet_baseline(self, tmp_path):
        declaration = parse_config_text(json.dumps(BOARD), "board.json")
        merged = merge_sources(declaration, [
            _load(tmp_path, _fleet(sensors=("GPU0_TEMP",)), "fleet.json"),
            _load(tmp_path, _pdr(sensors=("GPU0_TEMP",)), "pdr.json")])
        assert merged.sources[0].kind == PDR_FORMAT
        assert merged.sources[0].supplied == ("GPU0_TEMP",)
        assert merged.sources[1].supplied == (), "the fleet baseline overrode a pdr/1"

    def test_the_order_is_the_ruling_and_not_the_argument_order(self, tmp_path):
        """Non-vacuity for the test above: passing them the other way round must
        give the same answer, or precedence is just whatever the shell typed."""
        declaration = parse_config_text(json.dumps(BOARD), "board.json")
        merged = merge_sources(declaration, [
            _load(tmp_path, _pdr(sensors=("GPU0_TEMP",)), "pdr.json"),
            _load(tmp_path, _fleet(sensors=("GPU0_TEMP",)), "fleet.json")])
        assert merged.sources[0].kind == PDR_FORMAT
        assert merged.sources[1].supplied == ()

    def test_precedence_uses_the_matchers_normalisation(self, tmp_path):
        """`SHARED_TEMP` and `Shared Temp` are one sensor to the matcher. Keeping
        both would expect it twice and report one permanently absent -- a false
        regression created by the merge itself."""
        declaration = parse_config_text(json.dumps(BOARD), "board.json")
        merged = merge_sources(declaration,
                               [_load(tmp_path, _pdr(sensors=("Shared Temp",)))])
        assert merged.sources[0].supplied == ()

    def test_a_source_that_supplied_nothing_is_still_recorded(self, tmp_path):
        """Usually the sign of a file pointed at the wrong platform, which is a fact
        the reader wants rather than one to drop for being empty."""
        declaration = parse_config_text(json.dumps(BOARD), "board.json")
        merged = merge_sources(declaration,
                               [_load(tmp_path, _pdr(sensors=("SHARED_TEMP",)))])
        assert len(merged.sources) == 1
        assert merged.sources[0].supplied == ()


class TestACandidateRefusesToBeConsumed:
    def test_a_missing_reviewed_marker_is_refused(self, tmp_path):
        payload = _pdr()
        del payload["reviewed"]
        with pytest.raises(DeclarationSourceError, match="CANDIDATE"):
            _load(tmp_path, payload)

    def test_an_explicit_null_marker_is_refused_the_same_way(self, tmp_path):
        """What the emitter writes. Absent and null are one case here on purpose:
        the emitted slot exists to be seen and filled, not to satisfy a reader."""
        with pytest.raises(DeclarationSourceError, match="CANDIDATE"):
            _load(tmp_path, _candidate())

    @pytest.mark.parametrize("marker,missing", [
        ({"by": "someone"}, "on"),
        ({"on": "2026-08-24"}, "by"),
        ({"by": "", "on": "2026-08-24"}, "by"),
    ])
    def test_a_half_filled_marker_is_refused(self, tmp_path, marker, missing):
        """The shape of clearing the gate rather than passing it."""
        with pytest.raises(DeclarationSourceError, match=missing):
            _load(tmp_path, _pdr(reviewed=marker))

    def test_a_complete_marker_is_accepted(self, tmp_path):
        """Non-vacuity: it is the INCOMPLETE marker that is refused."""
        source = _load(tmp_path, _pdr())
        assert source.reviewed_by == "an operator"
        assert source.reviewed_on == "2026-08-24"

    def test_the_refusal_names_the_file_and_the_fix(self, tmp_path):
        with pytest.raises(DeclarationSourceError) as raised:
            _load(tmp_path, _candidate(), "hgx.json")
        message = str(raised.value)
        assert "hgx.json" in message
        assert '"reviewed"' in message
        assert "unprovisioned board" in message, (
            "the refusal has to carry the reason, or the next person adds the "
            "marker without doing the review")


class TestTheCandidateEmitterAssertsNothing:
    def test_what_it_writes_is_refused_by_the_loader(self, tmp_path):
        """The producer and the consumer, wired to each other. A candidate that its
        own loader accepted would be a gate with nothing behind it."""
        walk = _walk("GPU0_TEMP", "GPU1_TEMP")
        walk.captured_at = "2026-08-24T09:00:00+00:00"
        payload = candidate_from_walk(walk, platform="HGX-H100", firmware="1.03.05",
                                      source_path="walk.json")
        assert payload["reviewed"] is None
        with pytest.raises(DeclarationSourceError, match="CANDIDATE"):
            _load(tmp_path, payload)

    def test_adding_the_marker_is_the_only_thing_needed(self, tmp_path):
        """The other half. If the emitted file were invalid for some further reason,
        the reviewer would learn that one refusal at a time."""
        walk = _walk("GPU0_TEMP")
        walk.captured_at = "2026-08-24T09:00:00+00:00"
        payload = candidate_from_walk(walk, platform="HGX-H100", firmware="1.03.05",
                                      source_path="walk.json")
        payload["reviewed"] = {"by": "an operator", "on": "2026-08-24"}
        assert _load(tmp_path, payload).supplied == ()

    def test_an_incomplete_walk_cannot_become_a_declaration(self):
        walk = _walk("GPU0_TEMP")
        walk.captured_at = "2026-08-24T09:00:00+00:00"
        walk.errors = [("/redfish/v1/Chassis/2", "timeout")]
        with pytest.raises(DeclarationSourceError, match="did not complete"):
            candidate_from_walk(walk, platform="x", firmware=None, source_path="w")

    def test_an_empty_walk_cannot_become_a_declaration(self):
        """The founding hazard stated directly: an empty declaration reads clean
        against every machine."""
        walk = _walk()
        walk.captured_at = "2026-08-24T09:00:00+00:00"
        with pytest.raises(DeclarationSourceError, match="reads clean"):
            candidate_from_walk(walk, platform="x", firmware=None, source_path="w")

    def test_an_unstamped_walk_cannot_become_a_declaration(self):
        """Found by running the emitter and feeding its output back to the loader:
        the bletchley fixture predates `captured_at`, so the first version wrote a
        candidate its own loader refused. Stamping it with NOW would date a snapshot
        to the moment somebody converted it."""
        walk = _walk("GPU0_TEMP")
        assert walk.captured_at
        walk.captured_at = None
        with pytest.raises(DeclarationSourceError, match="no capture time"):
            candidate_from_walk(walk, platform="x", firmware=None, source_path="w")


class TestAbsenceFromAnAlternateSourceIsARegression:
    """The rule that would have shipped silently.

    `diff` sets aside declarations whose entity-manager `Type` does not produce a
    reading. These sources have no entity-manager Type, so every entry would
    classify `UNRECOGNISED` -- counted, printed, and never once able to fail a gate.
    A feature built to make GPU sensors gateable would have made them ungateable in
    a way that reads exactly like working.
    """

    def _report(self, tmp_path, *live: str):
        declaration = parse_config_text(json.dumps(BOARD), "board.json")
        merged = merge_sources(declaration, [_load(tmp_path, _pdr())])
        return compare(merged, _walk(*live))

    def test_a_pdr_sensor_that_is_absent_is_a_regression(self, tmp_path):
        report = self._report(tmp_path, "INLET_TEMP", "SHARED_TEMP", "GPU0_TEMP")
        absent = [f.sensor for f in report.regressions if f.kind == "declared_absent"]
        assert "GPU1_TEMP" in absent

    def test_it_is_not_filed_as_an_unrecognised_type(self, tmp_path):
        """The failure mode, asserted directly. `unrecognised_type` is reported and
        never asserted about, which is correct for entity-manager and silent here."""
        report = self._report(tmp_path, "INLET_TEMP", "SHARED_TEMP", "GPU0_TEMP")
        assert report.counts()["unrecognised_type"] == 0

    def test_a_pdr_sensor_that_is_present_stops_being_undeclared(self, tmp_path):
        """The other half of the feature: these sensors used to land in the reverse
        direction, which is true and unhelpful at fleet scale."""
        report = self._report(tmp_path, "INLET_TEMP", "SHARED_TEMP",
                              "GPU0_TEMP", "GPU1_TEMP")
        assert report.counts()["undeclared_present"] == 0
        assert report.regressions == []

    def test_the_entity_manager_type_filter_still_applies_to_entity_manager(self,
                                                                            tmp_path):
        """Non-vacuity in the dangerous direction. The exclusion is switched off for
        alternate sources and must stay on for the population it was built for."""
        board = {"Name": "Board", "Exposes": [
            {"Name": "FAN_PID", "Type": "Pid"},
            {"Name": "INLET_TEMP", "Type": "TMP75"}]}
        declaration = parse_config_text(json.dumps(board), "board.json")
        merged = merge_sources(declaration, [_load(tmp_path, _pdr())])
        report = compare(merged, _walk("INLET_TEMP", "GPU0_TEMP", "GPU1_TEMP"))
        assert report.counts()["not_a_sensor"] == 1
        assert report.regressions == []


class TestTheFleetBaselineSaysItIsADowngrade:
    def test_the_provenance_line_says_so_in_words(self, tmp_path):
        source = _load(tmp_path, _fleet())
        assert source.is_downgrade
        assert "last resort" in source.provenance_line()
        assert "derived from a fleet" in source.provenance_line()

    def test_a_pdr_does_not_carry_the_downgrade_sentence(self, tmp_path):
        source = _load(tmp_path, _pdr())
        assert not source.is_downgrade
        assert "last resort" not in source.provenance_line()

    def test_it_must_say_what_it_was_derived_from(self, tmp_path):
        payload = _fleet()
        del payload["derived_from"]
        with pytest.raises(DeclarationSourceError, match="derived_from"):
            _load(tmp_path, payload)

    def test_a_pdr_must_say_which_firmware(self, tmp_path):
        """Required of a `pdr/1` and not of a fleet baseline: discovered inventory
        moves with firmware, and a baseline spans firmware levels by construction."""
        payload = _pdr()
        del payload["firmware"]
        with pytest.raises(DeclarationSourceError, match="firmware"):
            _load(tmp_path, payload)

    def test_each_required_field_explains_itself(self, tmp_path):
        """One shared sentence covering four keys was the first cut, and it produced
        the `platform` reasoning under a missing `captured_at` -- a message that
        reads as authoritative and sends the reader to the wrong line."""
        payload = _pdr()
        del payload["captured_at"]
        with pytest.raises(DeclarationSourceError, match="which moment"):
            _load(tmp_path, payload)


class TestTheLoaderRefusesWhatCannotBeConsumed:
    def test_an_unknown_format_is_refused_by_name(self, tmp_path):
        with pytest.raises(DeclarationSourceError, match="pdr/1"):
            _load(tmp_path, _pdr(format="bmc-sensor-audit/walk/1"))

    def test_an_empty_sensor_list_is_refused(self, tmp_path):
        with pytest.raises(DeclarationSourceError, match="reads clean"):
            _load(tmp_path, _pdr(sensors=()))

    def test_a_sensor_declared_twice_is_refused(self, tmp_path):
        payload = _pdr()
        payload["sensors"].append({"name": "gpu0 temp"})
        with pytest.raises(DeclarationSourceError, match="declared twice"):
            _load(tmp_path, payload)

    def test_a_missing_platform_is_refused(self, tmp_path):
        payload = _pdr()
        del payload["platform"]
        with pytest.raises(DeclarationSourceError, match="wrong machine"):
            _load(tmp_path, payload)

    def test_an_unparseable_file_names_the_file(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text('{"format": ')
        with pytest.raises(DeclarationSourceError, match="broken.json"):
            load_declaration_source(str(path))

    def test_a_threshold_slot_it_cannot_place_is_refused(self, tmp_path):
        payload = _pdr()
        payload["sensors"][0]["thresholds"] = {"sideways/critical": 1.0}
        with pytest.raises(DeclarationSourceError, match="sideways/critical"):
            _load(tmp_path, payload)

    def test_thresholds_survive_into_the_declaration(self, tmp_path):
        """Non-vacuity for the slot check: the slots that ARE placeable become real
        bounds, or the format carries thresholds nothing ever compares."""
        source = _load(tmp_path, _pdr())
        threshold = source.sensors[0].thresholds[0]
        assert (threshold.bound, threshold.level) == ("upper", "critical")
        assert threshold.direction == "greater than"
        assert threshold.value == 95.0

    def test_unknown_keys_are_ignored(self, tmp_path):
        """The `/1` rule: a reader ignores keys it does not know. `fleet-baseline/1`
        is defined downstream, and what is defined here is the subset consumed."""
        payload = _fleet(something_the_fleet_layer_needs={"a": 1})
        assert _load(tmp_path, payload).platform == "HGX-H100"


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "bmc_sensor_audit.cli", *argv],
                          capture_output=True, text=True,
                          env={"PYTHONPATH": str(ROOT / "src"),
                               "PATH": "/usr/bin:/bin"})


class TestTheCommandLine:
    @pytest.fixture
    def board(self, tmp_path):
        path = tmp_path / "board.json"
        path.write_text(json.dumps(BOARD))
        return str(path)

    @pytest.fixture
    def walk(self, tmp_path):
        path = tmp_path / "walk.json"
        walked = _walk("INLET_TEMP", "SHARED_TEMP", "GPU0_TEMP", "GPU1_TEMP")
        path.write_text(json.dumps(walked.to_dict(), indent=2))
        return str(path)

    def test_a_candidate_is_refused_before_anything_is_compared(self, tmp_path, board,
                                                               walk):
        result = _run("coverage", "--config", board, "--walk", walk,
                      "--declaration", _write(tmp_path, "c.json", _candidate()))
        assert result.returncode == 2
        assert "CANDIDATE" in result.stderr
        assert "Sensor coverage" not in result.stdout, (
            "a refused declaration must not produce a report; half a population "
            "read as a whole one is the outcome the refusal exists to prevent")

    def test_the_provenance_sentence_appears_in_the_report(self, tmp_path, board,
                                                           walk):
        result = _run("coverage", "--config", board, "--walk", walk,
                      "--declaration", _write(tmp_path, "p.json", _pdr()))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "bmc-sensor-audit/pdr/1" in result.stdout
        assert "platform HGX-H100" in result.stdout
        assert "firmware 1.03.05" in result.stdout
        assert "reviewed by an operator on 2026-08-24" in result.stdout

    def test_the_downgrade_sentence_appears_for_a_fleet_baseline(self, tmp_path,
                                                                 board, walk):
        result = _run("coverage", "--config", board, "--walk", walk,
                      "--declaration", _write(tmp_path, "f.json",
                                              _fleet(sensors=("GPU0_TEMP",
                                                              "GPU1_TEMP"))))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "last resort" in result.stdout

    def test_a_run_without_one_claims_no_help(self, tmp_path, board, walk):
        """Non-vacuity in the direction that matters: the provenance block must be
        absent when the manufacturer's files were the only input."""
        result = _run("coverage", "--config", board, "--walk", walk)
        assert "other than entity-manager" not in result.stdout

    def test_the_json_carries_the_provenance_as_fields(self, tmp_path, board, walk):
        result = _run("coverage", "--config", board, "--walk", walk, "--json",
                      "--declaration", _write(tmp_path, "p.json", _pdr()))
        payload = json.loads(result.stdout)
        source = payload["declaration_sources"][0]
        assert source["format"] == PDR_FORMAT
        assert source["reviewed_by"] == "an operator"
        assert source["downgrade"] is False
        assert source["sensors_supplied"] == ["GPU0_TEMP", "GPU1_TEMP"]
        assert "platform HGX-H100" in source["provenance"]

    def test_the_json_omits_the_key_when_nothing_was_layered(self, board, walk):
        result = _run("coverage", "--config", board, "--walk", walk, "--json")
        assert "declaration_sources" not in json.loads(result.stdout)

    def test_declare_prints_the_provenance_too(self, tmp_path, board):
        """So an operator can check a file loads and see what it claims BEFORE
        pointing a gate at it."""
        result = _run("declare", "--config", board,
                      "--declaration", _write(tmp_path, "p.json", _pdr()))
        assert result.returncode == 0, result.stderr
        assert "bmc-sensor-audit/pdr/1" in result.stdout

    def test_from_walk_writes_a_candidate(self, tmp_path, walk):
        out = tmp_path / "candidate.json"
        result = _run("declare", "--from-walk", walk, "--candidate",
                      "--platform", "HGX-H100", "--firmware", "1.03.05",
                      "--out", str(out))
        assert result.returncode == 0, result.stderr
        payload = json.loads(out.read_text())
        assert payload["format"] == PDR_FORMAT
        assert payload["reviewed"] is None
        assert {s["name"] for s in payload["sensors"]} == {
            "INLET_TEMP", "SHARED_TEMP", "GPU0_TEMP", "GPU1_TEMP"}
        assert "REFUSED" in result.stdout

    @pytest.mark.parametrize("drop", ["--candidate", "--out", "--platform"])
    def test_from_walk_requires_its_companions(self, tmp_path, walk, drop):
        argv = ["declare", "--from-walk", walk, "--candidate",
                "--platform", "HGX-H100", "--out", str(tmp_path / "c.json")]
        if drop == "--candidate":
            argv.remove("--candidate")
        else:
            index = argv.index(drop)
            del argv[index:index + 2]
        result = _run(*argv)
        assert result.returncode == 2
        assert drop in result.stderr

    def test_config_and_from_walk_are_mutually_exclusive(self, board, walk):
        result = _run("declare", "--config", board, "--from-walk", walk)
        assert result.returncode == 2
        assert "not allowed with" in result.stderr


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
