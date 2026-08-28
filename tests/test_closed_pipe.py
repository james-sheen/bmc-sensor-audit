"""`| head` is not a fault in the machine being audited.

**The exit code is the claim, and the claim is about a BMC.** A reader that
stops reading has said something about itself, not about the hardware, so the
code this tool returns must not move when somebody pipes a report into `head`.

Before this was guarded, it moved two different ways. A report long enough to
fill the pipe buffer raised `BrokenPipeError` out of `print`, which escaped
`main` and left Python to exit `1` -- and `1` in this vocabulary means FINDINGS,
so a truncated report was indistinguishable from a complete one to anything
reading the code. A short report failed later instead, at the interpreter's
shutdown flush, printing `Exception ignored` and exiting `120`, which is outside
the vocabulary altogether. A fleet collector reads that as INCOMPLETE and files
a machine as unaudited because somebody piped it into `head`.

`CertificatePinError` was added to `REFUSALS` after the same shape: a refusal
that crashed, and the consumer saw `1`. This is that lesson applied to the other
end of the program, and it deliberately does NOT join `REFUSALS` -- that tuple
returns `2`, which would turn a clean run piped to `head` into an incomplete
audit. A closed pipe keeps whatever verdict was already computed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bmc_sensor_audit.inventory.redfish import WALK_FORMAT  # noqa: E402

#: Comfortably past a 64KB pipe buffer once rendered, so the failure lands in
#: `print` rather than at the shutdown flush. Both paths are exercised below.
WIDE = 700


def _walk(names) -> dict:
    return {
        "format": WALK_FORMAT,
        "chassis": ["/redfish/v1/Chassis/1"],
        "shapes_seen": ["sensors"],
        "errors": [],
        "captured_at": "2026-08-28T00:00:00+00:00",
        "fields_observed": True,
        "latencies": [],
        "sensors": [{"name": name,
                     "path": f"/redfish/v1/Chassis/1/Sensors/s{index}",
                     "reading": 41.0, "units": "Cel", "state": "Enabled",
                     "health": "OK", "shape": "sensors", "resource": "Sensor",
                     "thresholds": {"upper/critical": 95.0}}
                    for index, name in enumerate(names)],
    }


@pytest.fixture(scope="module")
def wide(tmp_path_factory):
    """A long report whose verdict is CLEAN, and the reason it is that way.

    **An unhandled `BrokenPipeError` makes Python exit `1`, and `1` is
    FINDINGS.** So a long report that legitimately exits `1` cannot tell a
    preserved verdict from a crash -- the two agree by coincidence, and the
    assertion passes against the unguarded code. This fixture walks sensors the
    board never declared: every one is enumerated, so the report is long, and
    undeclared extras are not findings, so the verdict is `0`. Now the crash
    code and the real code differ and the test can see the difference.
    """
    where = tmp_path_factory.mktemp("wide")
    walk, board = where / "walk.json", where / "board.json"
    names = [f"FOUND_{index:04d}" for index in range(WIDE)]
    walk.write_text(json.dumps(_walk(names)))
    board.write_text(json.dumps(
        {"Exposes": [{"Name": name, "Type": "TMP75"} for name in names[:3]]}))
    return ["coverage", "--config", str(board), "--walk", str(walk)]


@pytest.fixture(scope="module")
def wide_findings(tmp_path_factory):
    """The same length, with the verdict that collides with the crash code.

    Kept because FINDINGS is the verdict an operator most often pipes into
    `head`, and the guard has to hold for it too -- but the assertion that
    carries the weight is the one over `wide`, above.
    """
    where = tmp_path_factory.mktemp("findings")
    walk, board = where / "walk.json", where / "board.json"
    walk.write_text(json.dumps(_walk(f"PRESENT_{i:04d}" for i in range(WIDE))))
    board.write_text(json.dumps(
        {"Exposes": [{"Name": f"DECLARED_{i:04d}", "Type": "TMP75"}
                     for i in range(WIDE)]}))
    return ["coverage", "--config", str(board), "--walk", str(walk)]


@pytest.fixture(scope="module")
def narrow(tmp_path_factory):
    """A run whose report is short, and whose verdict is CLEAN."""
    where = tmp_path_factory.mktemp("narrow")
    walk, board = where / "walk.json", where / "board.json"
    names = [f"AGREED_{i:02d}" for i in range(3)]
    walk.write_text(json.dumps(_walk(names)))
    board.write_text(json.dumps(
        {"Exposes": [{"Name": n, "Type": "TMP75"} for n in names]}))
    return ["coverage", "--config", str(board), "--walk", str(walk)]


def _unpiped(argv) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "bmc_sensor_audit.cli", *argv],
                          capture_output=True, text=True,
                          env={"PYTHONPATH": str(ROOT / "src"),
                               "PATH": "/usr/bin:/bin"})


def _through_head(argv, lines: int) -> tuple[int, str]:
    """`(writer exit code, writer stderr)` -- the writer's, never the pipe's.

    The shell would report `head`'s code here, which is always 0 and would make
    every assertion below vacuous.
    """
    writer = subprocess.Popen(
        [sys.executable, "-m", "bmc_sensor_audit.cli", *argv],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
    reader = subprocess.Popen(["head", "-n", str(lines)], stdin=writer.stdout,
                              stdout=subprocess.DEVNULL)
    writer.stdout.close()
    reader.wait()
    stderr = writer.stderr.read()
    writer.stderr.close()
    writer.wait()
    return writer.returncode, stderr


class TestTheReportIsLongEnoughToReachTheDefect:
    def test_the_fixture_would_fill_a_pipe_buffer(self, wide):
        """Non-vacuity for everything below. A report that fits in the 64KB
        buffer never makes the writer notice the reader has gone, so a suite
        built on a short report would pass against the unguarded code."""
        rendered = _unpiped(wide)
        assert len(rendered.stdout) > 65536, (
            f"the report is {len(rendered.stdout)} bytes and cannot close a "
            f"pipe before the writer finishes")

    def test_and_its_verdict_differs_from_the_crash_code(self, wide):
        """`1` is what an unhandled exception exits with. If this fixture ever
        starts returning `1` the headline assertion below goes quiet without
        going red, so the difference is pinned here rather than assumed."""
        assert _unpiped(wide).returncode == 0

    def test_the_findings_fixture_is_long_too(self, wide_findings):
        assert len(_unpiped(wide_findings).stdout) > 65536
        assert _unpiped(wide_findings).returncode == 1


class TestAClosedPipeDoesNotMoveTheVerdict:
    def test_a_long_clean_report_keeps_its_exit_code(self, wide):
        """**The assertion the whole guard exists for.** Not *no traceback* --
        a run that printed nothing and exited 120 would satisfy that. And not
        over a FINDINGS report either, where the crash code would match by
        accident."""
        expected = _unpiped(wide).returncode
        code, _ = _through_head(wide, 10)
        assert code == expected, (
            f"piping the report changed the verdict from {expected} to {code}")

    def test_a_long_findings_report_keeps_its_exit_code(self, wide_findings):
        expected = _unpiped(wide_findings).returncode
        code, _ = _through_head(wide_findings, 10)
        assert code == expected

    def test_a_clean_run_stays_clean(self, narrow):
        """The direction a refusal-shaped fix would break. Mapping a closed
        pipe to INCOMPLETE would file a healthy machine as unaudited."""
        assert _unpiped(narrow).returncode == 0
        code, _ = _through_head(narrow, 1)
        assert code == 0

    def test_a_reader_that_leaves_immediately_is_also_clean(self, narrow):
        """The other failure path: too little output to fail during `print`, so
        it failed at the shutdown flush instead and exited 120."""
        code, stderr = _through_head(narrow, 0)
        assert code == 0, f"exit {code} for a clean audit whose reader left"
        assert "Exception ignored" not in stderr


class TestNothingIsPrintedAboutIt:
    @pytest.mark.parametrize("lines", [0, 1, 10])
    def test_no_traceback_reaches_the_operator(self, wide, lines):
        _, stderr = _through_head(wide, lines)
        assert "Traceback" not in stderr, stderr
        assert "BrokenPipeError" not in stderr, stderr

    def test_the_guard_is_not_a_refusal(self, narrow):
        """`REFUSALS` returns 2. A closed pipe must not join it, and the
        cheapest way to notice a future edit that adds it is to ask."""
        from bmc_sensor_audit.cli import REFUSALS
        assert BrokenPipeError not in REFUSALS
