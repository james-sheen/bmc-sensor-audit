"""The attestation format, its validator, and its manual — kept in agreement.

No engine required. `build_attestation` takes its `attest_fn` as an argument
precisely so the artifact can be built without one, and validating an artifact is
a Stage 1 operation by design: the person who RECEIVES one should not have to
install a detection engine to check it.

**The load-bearing test is `test_the_manual_documents_every_key_the_builder_writes`.**
The manual's job is to be authoritative, so the next reader counts fields there
rather than in the builder — and a field table maintained by hand drifts from the
code the first time a key is added. Asserted as set equality in both directions,
because a table listing a key that no longer exists misleads exactly as much as
one missing a key that does.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bmc_sensor_audit.detect.attestation import (  # noqa: E402
    ATTESTATION_FORMAT, build_attestation, validate_attestation)

MANUAL = ROOT / "docs" / "attestation-format.md"


class _Envelope:
    """The two engine objects the builder touches, without the engine."""

    def __init__(self, payload):
        self._payload = payload

    def to_dict(self):
        return self._payload


class _Manifest:
    sensors = ()

    def translate_finding(self, finding):
        return f"{finding['entity_id']} says something"


def _attested(problem_type):
    return _Envelope({
        "meta": {"schema_version": 1, "source": "live"},
        "evidence": [{
            "problem_type": problem_type, "entity_id": "e1", "axiom": "BOUNDEDNESS",
            "evidence": {"indicator": "reading", "value": 14.9, "threshold": 12.6,
                         "threshold_type": "critical", "bound": "upper"},
            "confidence": 1.0,
            "boundary": "engine-side evidence only; production records are v0.2"}]})


def _build(findings=None, declines=None):
    envelope = {
        "meta": {"schema_version": 1},
        "checked": {"invariants": 2, "entities": 1},
        "findings": findings if findings is not None else [
            {"entity_id": "e1", "problem_type": "threshold_exceeded:reading",
             "axiom": "BOUNDEDNESS", "severity": "critical",
             "reason": "reading exceeds critical threshold"}],
        "not_checked": declines if declines is not None else [
            {"entity_id": "e1", "axiom": "STABILITY",
             "reason": "insufficient_samples", "detail": "too few observations"}],
    }
    return build_attestation(None, envelope, {}, _Manifest(), target="t",
                             attest_fn=lambda s, pt: _attested(pt))


@pytest.fixture
def artifact():
    return _build()


class TestAValidArtifactValidates:
    def test_the_built_artifact_passes_its_own_validator(self, artifact):
        assert validate_attestation(artifact) == []

    def test_it_declares_the_format_the_validator_reads(self, artifact):
        assert artifact["format"] == ATTESTATION_FORMAT


class TestACleanBoardIsValid:
    """The distinction the validator exists to keep, and the one the inline
    version of these rules got wrong.

    `findings is non-empty` was originally asserted alongside the format rules,
    in a CI workflow that ran over a fixture known to produce findings. Promoting
    that check into the shipped validator would mean **a genuinely clean board
    failing validation** -- a healthy machine reported as a broken artifact, which
    is the inversion this whole project exists to prevent.
    """

    def test_an_artifact_with_no_findings_is_valid(self):
        clean = _build(findings=[])
        assert clean["findings"] == [] and clean["evidence"] == []
        assert validate_attestation(clean) == []

    def test_an_artifact_with_nothing_declined_is_valid(self):
        assert validate_attestation(_build(declines=[])) == []


class TestItRefusesEachWayTheArtifactCanBeUseless:
    """A check that has never refused anything is not evidence."""

    def _mutate(self, artifact, **changes):
        broken = copy.deepcopy(artifact)
        broken.update(changes)
        return broken

    def test_a_wrong_format_is_refused(self, artifact):
        problems = validate_attestation(self._mutate(artifact, format="other/1"))
        assert any("format" in p for p in problems)

    @pytest.mark.parametrize("key", ["findings", "not_checked", "evidence"])
    def test_a_missing_core_list_is_refused(self, artifact, key):
        broken = copy.deepcopy(artifact)
        del broken[key]
        assert any(key in p for p in validate_attestation(broken))

    def test_a_finding_without_its_measurement_is_refused(self, artifact):
        problems = validate_attestation(self._mutate(artifact, evidence=[]))
        assert any("measurement" in p for p in problems)

    def test_an_evidence_entry_with_an_empty_measurement_is_refused(self, artifact):
        broken = copy.deepcopy(artifact)
        broken["evidence"][0]["measurement"] = {}
        assert any("measurement" in p for p in validate_attestation(broken))

    def test_a_missing_boundary_is_refused_when_there_IS_evidence(self, artifact):
        """Conditional on purpose -- see `TestACleanBoardIsValid`. A run with no
        findings has no evidence and therefore no boundary to carry."""
        assert artifact["evidence"], "this test needs an artifact carrying evidence"
        problems = validate_attestation(
            self._mutate(artifact, engine={"schema_version": 1}))
        assert any("boundary" in p for p in problems)

    def test_a_missing_schema_version_is_refused(self, artifact):
        problems = validate_attestation(
            self._mutate(artifact, engine={"boundary": "x"}))
        assert any("schema_version" in p for p in problems)

    @pytest.mark.parametrize("key", ["unattested", "unread_feeds"])
    def test_a_missing_boundary_list_is_refused(self, artifact, key):
        """An absent list reads as *nothing was left out*, which is the one claim
        this format must never make silently."""
        broken = copy.deepcopy(artifact)
        del broken[key]
        assert any(key in p for p in validate_attestation(broken))

    def test_a_non_object_is_refused_without_a_cascade(self):
        """One fault named once. A type error followed by every consequence of it
        reports four problems where there is one."""
        problems = validate_attestation(["not", "an", "object"])
        assert len(problems) == 1


class TestTheManualAndTheBuilderAgree:
    def test_the_manual_documents_every_key_the_builder_writes(self, artifact):
        """Set equality both ways, derived from a real build rather than from a
        list typed into the test."""
        rows = re.findall(r"^\|\s*`([a-z_.]+)`\s*\|", MANUAL.read_text(),
                          re.MULTILINE)
        documented = {row.split(".")[0] for row in rows}
        written = set(artifact)
        assert documented == written, (
            f"the manual and the builder disagree. Only in the manual: "
            f"{sorted(documented - written)}. Only in the artifact: "
            f"{sorted(written - documented)}")

    def test_the_manual_states_the_versioning_rule(self):
        body = MANUAL.read_text()
        assert "may gain keys and may never change the meaning" in body
        assert ATTESTATION_FORMAT in body

    def test_the_manual_names_the_command_that_checks_one(self):
        assert "validate-attestation" in MANUAL.read_text()


class TestTheCommandLine:
    def _run(self, path):
        return subprocess.run(
            [sys.executable, "-m", "bmc_sensor_audit.cli",
             "validate-attestation", str(path)],
            capture_output=True, text=True,
            env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin",
                 "HOME": "/root"})

    def test_a_valid_artifact_exits_clean(self, tmp_path, artifact):
        path = tmp_path / "a.json"
        path.write_text(json.dumps(artifact))
        result = self._run(path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "valid" in result.stdout

    def test_an_invalid_artifact_exits_one_and_lists_every_problem(
            self, tmp_path, artifact):
        broken = copy.deepcopy(artifact)
        broken["format"] = "wrong/1"
        broken["evidence"] = []
        path = tmp_path / "a.json"
        path.write_text(json.dumps(broken))
        result = self._run(path)
        assert result.returncode == 1
        assert "format" in result.stderr and "measurement" in result.stderr

    def test_an_unreadable_file_is_two_not_one(self, tmp_path):
        """*Could not check* is not *checked and found wrong*, and the exit codes
        keep that distinction everywhere else in this tool."""
        assert self._run(tmp_path / "nope.json").returncode == 2

    def test_a_file_that_is_not_json_is_two(self, tmp_path):
        path = tmp_path / "a.json"
        path.write_text("{not json")
        assert self._run(path).returncode == 2

    def test_it_runs_without_the_engine_installed(self, tmp_path, artifact):
        """The recipient's path. Auditing an artifact must not require installing
        a detection engine."""
        path = tmp_path / "a.json"
        path.write_text(json.dumps(artifact))
        probe = subprocess.run(
            [sys.executable, "-c",
             "import bmc_sensor_audit.detect.attestation as m; "
             "import sys; sys.exit(0 if 'arbiter_engine' not in sys.modules else 1)"],
            capture_output=True, text=True,
            env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin",
                 "HOME": "/root"})
        assert probe.returncode == 0, "importing the module pulled in the engine"
        assert self._run(path).returncode == 0
