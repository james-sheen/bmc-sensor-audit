"""The walk format, its validator, its digest, and its manual — kept in agreement.

`walk/1` has been stamped on every capture and checked on every read since Stage 1.
What it did not have was a way for the person who RECEIVES a capture to check one,
or a statement of what may change inside `/1` for a downstream pin to pin to. Both
of those are the consumer's half of a versioned format, and neither can be supplied
by the producer asserting that it is careful.

**The load-bearing test is `test_the_manual_documents_every_key_the_writer_writes`.**
The manual's job is to be authoritative, so the next reader counts fields there
rather than in `Walk.to_dict` — and a field table maintained by hand drifts from the
code the first time a key is added. Asserted as set equality in both directions:
a table listing a key that no longer exists misleads exactly as much as one missing
a key that does.

**The validator's non-vacuity is asserted against real captures**, not against
hand-built payloads alone. Every vendored walk in this repository must validate, or
the rule being enforced is not the rule this tool writes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bmc_sensor_audit.inventory.redfish import (  # noqa: E402
    WALK_FORMAT, RedfishClient, validate_walk, walk_chassis, walk_digest,
    walk_from_dict)
from bmc_sensor_audit.testing.mock_redfish import MockBMC, serve  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
MANUAL = ROOT / "docs" / "walk-format.md"


def _bmc() -> MockBMC:
    bmc = MockBMC(shape="sensors")
    bmc.add("FAN0", reading=4200.0, units="RPM",
            upper_critical=9000.0, lower_critical=1000.0)
    bmc.add("INLET_TEMP", reading=24.0, upper_critical=45.0)
    return bmc


@pytest.fixture(scope="module")
def payload() -> dict:
    """A capture of a real walk over real HTTP, serialised the way `capture` does."""
    with serve(_bmc()) as url:
        return walk_chassis(RedfishClient(url)).to_dict()


def _vendored_walks() -> list[Path]:
    """Every capture in the fixture directory that declares the walk format.

    Selected by what each file DECLARES rather than by what it is named. A glob
    over a naming convention is a transcription in disguise, and this repository
    has already shipped one: `walk_*.json` missed two fixtures added the same day
    and named for what they are.
    """
    found = []
    for path in sorted(FIXTURES.rglob("*.json")):
        try:
            if json.loads(path.read_text()).get("format") == WALK_FORMAT:
                found.append(path)
        except (json.JSONDecodeError, AttributeError):
            continue
    return found


class TestTheValidatorAcceptsWhatThisToolWrites:
    def test_a_freshly_captured_walk_validates(self, payload):
        assert validate_walk(payload) == []

    @pytest.mark.parametrize("path", _vendored_walks(), ids=lambda p: p.name)
    def test_every_vendored_capture_validates(self, path):
        """Non-vacuity against the real population. A validator agreeing only with
        payloads written to satisfy it has been tested against itself."""
        assert validate_walk(json.loads(path.read_text())) == []

    def test_the_vendored_population_is_not_empty(self):
        """The parametrize above would pass by running zero cases."""
        assert len(_vendored_walks()) >= 2

    def test_a_capture_with_no_sensors_is_legal(self):
        """A chassis reporting no sensors is a real machine. Refusing it here would
        fail a file `capture` writes, and a validator that rejects valid input is one
        people learn to route around -- taking the malformed cases with it."""
        assert validate_walk({"format": WALK_FORMAT, "sensors": []}) == []

    def test_an_incomplete_capture_is_legal(self):
        """`capture` writes partial walks deliberately: they are the record of WHICH
        subtree failed. Whether a partial walk is usable is `--require-complete`."""
        assert validate_walk({"format": WALK_FORMAT, "sensors": [],
                              "errors": [["/redfish/v1/Chassis/1", "timeout"]]}) == []

    def test_the_fields_that_predate_themselves_may_be_absent(self):
        """`captured_at`, `latencies` and `fields_observed` are all missing from
        captures written before those fields existed, and the reader handles that.
        A validator stricter than the reader refuses files this tool still reads."""
        assert validate_walk({"format": WALK_FORMAT,
                              "sensors": [{"name": "FAN0", "reading": 1.0}]}) == []


class TestTheValidatorRefusesWhatTheReaderCannotUse:
    def test_a_non_object_is_named_as_one(self):
        assert validate_walk([1, 2, 3]) == [
            "the walk is list, not an object"]

    def test_a_foreign_format_is_named(self):
        problems = validate_walk({"format": "something/else", "sensors": []})
        assert len(problems) == 1
        assert WALK_FORMAT in problems[0]

    def test_a_missing_sensors_list_stops_further_reporting(self):
        """Everything below iterates it, so reporting the type error and then every
        consequence of it names one fault many times over."""
        problems = validate_walk({"format": WALK_FORMAT})
        assert problems == ["'sensors' is missing or is not a list"]

    def test_a_sensor_without_a_name_is_refused(self):
        """The reader indexes this key directly, so a capture without it raises
        rather than degrading. Naming it turns an unreadable file into a reported
        one."""
        problems = validate_walk({"format": WALK_FORMAT, "sensors": [{"reading": 1.0}]})
        assert len(problems) == 1
        assert "carries no 'name'" in problems[0]

    def test_a_reading_that_is_not_a_number_is_refused(self):
        problems = validate_walk({"format": WALK_FORMAT,
                                  "sensors": [{"name": "FAN0", "reading": "4200"}]})
        assert ["non-numeric 'reading'" in p for p in problems].count(True) == 1

    def test_a_boolean_reading_is_refused(self):
        """`bool` is a subclass of `int`, so it passes an isinstance number test. A
        capture carrying `"reading": true` would validate and then rehydrate into a
        sensor reading 1.0 -- a number nothing measured, in the field the whole tool
        is pointed at."""
        problems = validate_walk({"format": WALK_FORMAT,
                                  "sensors": [{"name": "FAN0", "reading": True}]})
        assert ["non-numeric 'reading'" in p for p in problems].count(True) == 1

    def test_a_threshold_slot_this_build_does_not_write_is_refused(self):
        problems = validate_walk({
            "format": WALK_FORMAT,
            "sensors": [{"name": "FAN0", "thresholds": {"sideways/critical": 9000.0}}]})
        assert len(problems) == 1
        assert "sideways/critical" in problems[0]

    def test_a_threshold_that_is_not_a_number_is_refused(self):
        problems = validate_walk({
            "format": WALK_FORMAT,
            "sensors": [{"name": "FAN0", "thresholds": {"upper/critical": "hot"}}]})
        assert len(problems) == 1
        assert "is not a number" in problems[0]

    def test_undeclared_properties_contradicting_the_flag_are_refused(self):
        """The one cross-field rule, and the reason it is worth having. The walk
        says nobody compared these objects against the schema; the sensor says
        somebody did and found something. A reader trusting the flag reports a clean
        board while the file in front of it names the drift."""
        problems = validate_walk({
            "format": WALK_FORMAT, "fields_observed": False,
            "sensors": [{"name": "FAN0", "undeclared": ["SerialNumber"]}]})
        assert len(problems) == 1
        assert "nobody looked" in problems[0]

    def test_the_same_walk_with_the_flag_set_is_accepted(self):
        """Non-vacuity for the rule above: it is the CONTRADICTION that is refused,
        not the presence of undeclared properties."""
        assert validate_walk({
            "format": WALK_FORMAT, "fields_observed": True,
            "sensors": [{"name": "FAN0", "undeclared": ["SerialNumber"]}]}) == []

    def test_every_problem_is_reported_at_once(self):
        """Returned rather than raised, so a file is fixed in one pass instead of
        learning one fault per run. The attestation validator had exactly this
        defect: a wrong `format` masked every other problem behind it."""
        problems = validate_walk({
            "format": "wrong", "captured_at": 17,
            "sensors": [{"name": "FAN0", "reading": "x"}, {"reading": 1.0}]})
        assert len(problems) == 4


class TestTheThresholdVocabularyIsDerived:
    def test_every_slot_the_writer_produces_validates(self, payload):
        """The validator's slot vocabulary is derived from the two threshold maps in
        the walker rather than listed again beside them. This is the check that the
        derivation is live: a mapping added to the walker must not become a slot its
        own validator refuses."""
        slots = {slot for sensor in payload["sensors"]
                 for slot in (sensor.get("thresholds") or {})}
        assert slots, "the fixture BMC declares no thresholds; this proves nothing"
        for slot in slots:
            assert validate_walk({"format": WALK_FORMAT,
                                  "sensors": [{"name": "S", "thresholds": {slot: 1.0}}]}) == []


class TestTheManualAndTheWriterAgree:
    def _documented(self) -> set[str]:
        """Keys named in the manual's field table, read out of the table itself."""
        keys = set()
        for line in MANUAL.read_text().splitlines():
            match = re.match(r"^\| `([a-z_\[\]\.]+)` \|", line)
            if match:
                keys.add(match.group(1))
        return keys

    def test_the_manual_documents_every_key_the_writer_writes(self, payload):
        """Set equality in both directions. A table listing a key that no longer
        exists misleads exactly as much as one missing a key that does -- and the
        reader who counts fields will count them here, not in the code."""
        written = set(payload) | {
            f"sensors[].{key}" for sensor in payload["sensors"] for key in sensor}
        # `undeclared` is written only when non-empty, so a walk of a schema-clean
        # mock never produces it. Named explicitly rather than dropped from the
        # comparison: the manual documents it, and the reason it is absent here is
        # a property of the fixture, not of the format.
        written.add("sensors[].undeclared")
        assert self._documented() == written

    def test_the_manual_states_the_versioning_rule(self):
        body = MANUAL.read_text()
        assert "may gain keys and may never change the meaning of one" in body, (
            "the stability statement is what a downstream pin pins TO; without it "
            "`walk/1` is a string rather than a contract")

    def test_the_manual_refuses_an_identity_field_in_words(self):
        assert "No identity field enters `walk/1`, ever." in MANUAL.read_text()


class TestTheDigestIsAContentHandleAndNotAnIdentity:
    def test_it_is_the_sha256_of_the_bytes(self, tmp_path):
        """Recomputable with `sha256sum`, in any language, by a recipient who has
        neither this tool nor a reason to trust it."""
        path = tmp_path / "walk.json"
        path.write_bytes(b'{"format": "x"}')
        expected = hashlib.sha256(b'{"format": "x"}').hexdigest()
        assert walk_digest(path.read_bytes()) == f"sha256:{expected}"

    def test_str_and_bytes_agree(self):
        assert walk_digest("abc") == walk_digest(b"abc")

    def test_one_changed_reading_changes_the_handle(self, payload):
        moved = copy.deepcopy(payload)
        moved["sensors"][0]["reading"] = 9999.0
        assert (walk_digest(json.dumps(payload, indent=2))
                != walk_digest(json.dumps(moved, indent=2)))

    def test_the_handle_carries_no_machine_identity(self, payload):
        """It says which capture, never which machine. The binding to a unit happens
        outside, in the layer whose job is to name things."""
        assert re.fullmatch(r"sha256:[0-9a-f]{64}",
                            walk_digest(json.dumps(payload)))


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "bmc_sensor_audit.cli", *argv],
                          capture_output=True, text=True,
                          env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})


class TestTheCommandLine:
    def test_a_vendored_capture_validates_and_exits_clean(self):
        result = _run("validate-walk", str(FIXTURES / "walk_qemu_bletchley.json"))
        assert result.returncode == 0, result.stderr
        assert f"valid {WALK_FORMAT}" in result.stdout

    def test_a_malformed_capture_exits_1_and_names_the_problem(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"format": WALK_FORMAT,
                                    "sensors": [{"reading": 1.0}]}))
        result = _run("validate-walk", str(path))
        assert result.returncode == 1
        assert "carries no 'name'" in result.stderr

    def test_an_unparseable_file_exits_2_not_1(self, tmp_path):
        """A file that could not be read is a different claim from a file that was
        read and found wrong. Conflating them fails a good capture on a bad disk."""
        path = tmp_path / "truncated.json"
        path.write_text('{"format": "bmc-sensor-audit/walk/1", "sen')
        result = _run("validate-walk", str(path))
        assert result.returncode == 2
        assert "not parseable as JSON" in result.stderr

    def test_a_missing_file_exits_2(self, tmp_path):
        result = _run("validate-walk", str(tmp_path / "nothing.json"))
        assert result.returncode == 2
        assert "cannot read" in result.stderr

    def test_an_incomplete_walk_is_reported_and_still_exits_clean(self, tmp_path):
        path = tmp_path / "partial.json"
        path.write_text(json.dumps({"format": WALK_FORMAT, "sensors": [],
                                    "errors": [["/redfish/v1/Chassis/1", "timeout"]]}))
        result = _run("validate-walk", str(path))
        assert result.returncode == 0
        assert "INCOMPLETE" in result.stdout

    def test_require_complete_floors_the_same_walk_at_2(self, tmp_path):
        """The flag is the ask, and asking is what makes the rule apply. The same
        shape `--strict-fields` uses, so the two flags mean one sentence."""
        path = tmp_path / "partial.json"
        path.write_text(json.dumps({"format": WALK_FORMAT, "sensors": [],
                                    "errors": [["/redfish/v1/Chassis/1", "timeout"]]}))
        result = _run("validate-walk", str(path), "--require-complete")
        assert result.returncode == 2
        assert "completeness was required" in result.stderr

    def test_require_complete_leaves_a_whole_walk_clean(self):
        """Non-vacuity: the flag floors on incompleteness and on nothing else."""
        result = _run("validate-walk", str(FIXTURES / "walk_qemu_bletchley.json"),
                      "--require-complete")
        assert result.returncode == 0, result.stderr

    def test_the_two_sides_print_the_same_handle(self, tmp_path):
        """The whole point of the digest: the side that WRITES the file and the side
        that RECEIVES it agree without having to agree on a canonicalisation."""
        path = tmp_path / "walk.json"
        with serve(_bmc()) as url:
            capture = _run("capture", "--target", url, "--out", str(path),
                           "--print-digest")
        assert capture.returncode == 0, capture.stderr
        written = re.search(r"digest\s+(sha256:[0-9a-f]{64})", capture.stdout)
        assert written, capture.stdout

        validated = _run("validate-walk", str(path), "--print-digest")
        assert validated.returncode == 0, validated.stderr
        read_back = re.search(r"digest\s+(sha256:[0-9a-f]{64})", validated.stdout)
        assert read_back, validated.stdout
        assert written.group(1) == read_back.group(1)

    def test_the_handle_is_the_one_sha256sum_would_give(self, tmp_path):
        path = tmp_path / "walk.json"
        with serve(_bmc()) as url:
            result = _run("capture", "--target", url, "--out", str(path),
                          "--print-digest")
        printed = re.search(r"digest\s+(sha256:[0-9a-f]{64})", result.stdout).group(1)
        assert printed == f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"

    def test_capture_without_the_flag_prints_no_handle(self, tmp_path):
        """Additive, and off unless asked. A digest on every capture would be one
        more line in an output people already read past."""
        path = tmp_path / "walk.json"
        with serve(_bmc()) as url:
            result = _run("capture", "--target", url, "--out", str(path))
        assert "digest" not in result.stdout

    def test_the_captured_file_rehydrates(self, tmp_path):
        """The digest is over the bytes, so it is only useful if those bytes are a
        walk this tool reads back."""
        path = tmp_path / "walk.json"
        with serve(_bmc()) as url:
            _run("capture", "--target", url, "--out", str(path))
        walk = walk_from_dict(json.loads(path.read_text()))
        assert {s.name for s in walk} == {"FAN0", "INLET_TEMP"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
