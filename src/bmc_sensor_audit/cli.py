"""Command line entry point for Stage 1.

    bmc-sensor-audit coverage --config <path> --target https://<bmc> [--insecure]
    bmc-sensor-audit coverage --config <path> --walk recorded-walk.json
    bmc-sensor-audit declare  --config <path>

`--config` accepts a file or a directory, and a directory is walked recursively,
because a platform's declaration is normally several files (baseboard, chassis,
front panel) and asking an operator to enumerate them invites them to miss one.

**Exit codes are the CI interface**: 0 clean, 1 regressions found, 2 the run
could not be completed. 2 is distinct from 1 on purpose -- a pipeline that
treats "could not reach the BMC" as "sensors are missing" will fail a good
firmware image, and it only has to do that once before nobody trusts the gate.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from .inventory.diff import compare
from .inventory.entity_manager import load_declaration
from .inventory.redfish import RedfishClient, Walk, walk_chassis, walk_from_dict
from .report import as_json, as_text

EXIT_CLEAN, EXIT_REGRESSION, EXIT_INCOMPLETE = 0, 1, 2


def _load_recorded_walk(path: str) -> Walk:
    """Rehydrate a walk from a recorded fixture.

    Recording once and diffing repeatedly is how the firmware-upgrade gate works:
    capture before, capture after, compare both against the config. It is also
    how the test suite runs with no hardware in the room.
    """
    return walk_from_dict(json.loads(Path(path).read_text()))


def _client(args: argparse.Namespace) -> RedfishClient:
    return RedfishClient(args.target, username=args.username, password=args.password,
                         verify_tls=not args.insecure, timeout=args.timeout)


def _cmd_capture(args: argparse.Namespace) -> int:
    """Record a walk to disk, for diffing later or for a before/after gate."""
    walk = walk_chassis(_client(args))
    Path(args.out).write_text(json.dumps(walk.to_dict(), indent=2))
    print(f"wrote {len(walk)} sensor(s) to {args.out}")
    print(f"  chassis     {len(walk.chassis)}")
    print(f"  tree shapes {sorted(walk.shapes_seen) or '(none found)'}")
    if walk.latencies:
        times = sorted(t for _, t in walk.latencies)
        slowest_path, slowest = max(walk.latencies, key=lambda pair: pair[1])
        # The TAIL, not the mean. A Redfish stack that has started to struggle
        # answers most requests normally and a few very slowly, and a mean over a
        # hundred fetches hides exactly that.
        print(f"  fetches     {len(times)}  median {times[len(times)//2]:.3f}s  "
              f"slowest {slowest:.3f}s")
        print(f"    slowest was {slowest_path}")
    if walk.divergence:
        print(f"  {len(walk.divergence)} sensor(s) present on only one interface")
    if not walk.complete:
        # Written anyway: a partial capture is still evidence, and deleting it
        # loses the record of WHICH subtree failed. But it must not be mistaken
        # for a baseline, and a diff against it withholds absence findings.
        print(f"  ** INCOMPLETE -- {len(walk.errors)} fetch(es) failed **")
        for path, reason in walk.errors[:5]:
            print(f"     {path}: {reason}")
        return EXIT_INCOMPLETE
    return EXIT_CLEAN


def _cmd_declare(args: argparse.Namespace) -> int:
    declaration = load_declaration(args.config)
    print(f"read {declaration.files_read} file(s) from {len(args.config)} path(s)")
    print(f"  sensors declared   {len(declaration):>5}")
    print(f"  templated names    {len(declaration.templated):>5}")
    print(f"  disabled in config {len(declaration.disabled):>5}")
    print(f"  anomalies          {len(declaration.anomalies):>5}")
    print(f"  unreadable files   {len(declaration.unreadable):>5}")
    for source, reason in declaration.unreadable:
        print(f"    {source}: {reason}")
    for anomaly in declaration.anomalies:
        print(f"  {anomaly}")

    # A file that parses and declares nothing is a THIRD state, and the summary
    # above cannot express it. Point this at a directory of JSON schemas and it
    # prints `read 22 file(s)` with `0 unreadable` -- every number honest, the
    # answer meaningless, and indistinguishable from a board that genuinely
    # declares nothing. That is the exact shape this tool exists to catch on
    # someone else's machine, and `coverage` already refuses it; `declare` was
    # reporting it as clean and exiting 0.
    #
    # The two causes are split because they have different fixes: a path that
    # matched no files is usually wrong, while a path that matched files
    # declaring nothing is usually pointed at the wrong KIND of directory.
    if not declaration.sensors:
        if declaration.files_read == 0:
            print("no files were read under the given paths -- check the path",
                  file=sys.stderr)
        else:
            print(f"{declaration.files_read} file(s) read, none of which declares "
                  "a sensor. Nothing here can be audited -- check the path names a "
                  "configuration directory and not, say, a schema directory.",
                  file=sys.stderr)
        return EXIT_INCOMPLETE

    # An unreadable config is not a clean board; it is an unknown one.
    return EXIT_INCOMPLETE if declaration.unreadable else EXIT_CLEAN


def _report_unreadable(declaration) -> int:
    """An unreadable config is not a clean board; it is an unknown one.

    Returns the exit code this fact floors the answer at, so a caller composes it with
    whatever else it found instead of choosing between them.

    `declare` has applied this rule at its own exit since the beginning. `coverage` and
    `detect` did not: both printed `cannot read: ... every sensor this file declares is
    unverifiable, not absent` and then exited 0, which is the single outcome that
    sentence rules out. Reported from outside against `detect`; `coverage` carried the
    same guard and the same hole. The case that matters is in neither report -- a real
    configuration directory with one corrupt file in it, where everything else audits
    normally and the gate goes green.

    Printed from here rather than from each exit so it is reached whether or not the
    optional engine extra is installed. At the exit it would be emitted only on the
    path that already had a reason to fail.
    """
    if not declaration.unreadable:
        return EXIT_CLEAN
    print(f"\n{len(declaration.unreadable)} configuration file(s) could not be read. "
          "The sensors they declare are unverifiable, not absent, so this run cannot "
          "report a clean board:", file=sys.stderr)
    for source, reason in declaration.unreadable:
        print(f"    {source}: {reason}", file=sys.stderr)
    return EXIT_INCOMPLETE


def _cmd_coverage(args: argparse.Namespace) -> int:
    declaration = load_declaration(args.config)
    if not declaration.sensors and not declaration.unreadable:
        print("no sensors declared by any file under the given paths", file=sys.stderr)
        return EXIT_INCOMPLETE
    unreadable_floor = _report_unreadable(declaration)

    if args.walk:
        walk = _load_recorded_walk(args.walk)
        target = args.walk
    else:
        walk = walk_chassis(_client(args))
        target = args.target

    report = compare(declaration, walk,
                     include_disabled_in_config=args.include_disabled)
    rendered = (as_json(report, target=target) if args.json
                else as_text(report, target=target))
    print(rendered)

    if not report.walk_complete:
        return EXIT_INCOMPLETE
    # Composed the way `detect` composes its two stages: the worse wins, and 2 outranks
    # 1 because could-not-read is a different claim from something-got-worse.
    stage1 = EXIT_REGRESSION if report.regressions else EXIT_CLEAN
    return max(stage1, unreadable_floor)


def _cmd_detect(args: argparse.Namespace) -> int:
    """Both stages in one run, and one exit code.

    Stage 1 answers presence; Stage 2 answers liveness for what is present. They are
    composed rather than merged, because they fail differently: a walk that could not
    complete is not a board with missing sensors, and neither is an engine that is not
    installed.
    """
    declaration = load_declaration(args.config)
    if not declaration.sensors and not declaration.unreadable:
        print("no sensors declared by any file under the given paths", file=sys.stderr)
        return EXIT_INCOMPLETE
    unreadable_floor = _report_unreadable(declaration)

    # `--walk` is repeatable and CHRONOLOGICAL, oldest first: stuck-at needs history,
    # and one walk is one sample. A live target gives exactly one.
    if args.walk:
        walks = [_load_recorded_walk(path) for path in args.walk]
        target = args.walk[-1]
    else:
        walks = [walk_chassis(_client(args))]
        target = args.target

    reports = [compare(declaration, walk,
                       include_disabled_in_config=args.include_disabled)
               for walk in walks]
    current = reports[-1]
    print(as_text(current, target=target))

    if not current.walk_complete:
        # An incomplete walk is not an empty machine, and it is not a model worth
        # feeding either. Stop before the engine sees a partial picture.
        print("\nwalk incomplete; liveness not evaluated", file=sys.stderr)
        return EXIT_INCOMPLETE

    try:
        import yaml
        from arbiter_engine.api import EngineSession, check, model_describe
    except ImportError as error:
        print(f"\nliveness needs the optional extra, which is not installed: {error}\n"
              "    pip install 'bmc-sensor-audit[detect]'\n"
              "Stage 1 coverage above is complete and unaffected.", file=sys.stderr)
        return EXIT_INCOMPLETE

    from .detect.feeder import evaluate, feed
    from .detect.generator import generate
    from .detect.supplemental import (SupplementalError, load_supplemental,
                                      unmatched_names)
    from .report import detect_as_text

    supplemental = None
    if args.supplemental:
        # A refusal here stops the run rather than degrading it. A supplemental file
        # that failed to load and carried on would produce a report with no
        # disagreements in it because nothing was ever compared -- and from the
        # outside that is identical to a board where every declared pair agrees.
        try:
            supplemental = load_supplemental(args.supplemental)
        except SupplementalError as error:
            print(f"\n{error}", file=sys.stderr)
            return EXIT_INCOMPLETE
        missing = unmatched_names(supplemental,
                                  {s.display_name for s in declaration.sensors})
        if missing:
            print(f"\n{args.supplemental} names {len(missing)} sensor(s) this "
                  f"configuration does not declare. A name that matches nothing "
                  f"creates no check, silently:", file=sys.stderr)
            for name in missing:
                print(f"    {name}", file=sys.stderr)
            return EXIT_INCOMPLETE

    model, manifest = generate(declaration, expect_variation=not args.no_stuck_at,
                               supplemental=supplemental)
    if args.model_out:
        Path(args.model_out).write_text(yaml.safe_dump(model))
    if args.manifest_out:
        Path(args.manifest_out).write_text(json.dumps(manifest.to_dict(), indent=2))

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        handle.write(yaml.safe_dump(model))
        model_path = handle.name
    session = EngineSession()
    session.load_model(model_path)

    feed_result = feed(session, manifest, reports)
    envelope = check(session).to_dict()
    described = model_describe(session).to_dict()
    outcome = evaluate(envelope, described, manifest,
                       strict_declines=args.strict_declines,
                       feed_result=feed_result)

    if args.attest_out:
        # After `check`, never before: `attest` refuses on an unchecked session with
        # `source: unavailable`, and that refusal reads a lot like a clean run.
        from arbiter_engine.api import attest

        from .detect.attestation import build_attestation
        Path(args.attest_out).write_text(json.dumps(
            build_attestation(session, envelope, described, manifest,
                              target=target, attest_fn=attest), indent=2))
    print(detect_as_text(outcome, feed_result))

    # Composed, not merged. The worse of the four wins, and `2` outranks `1` because
    # could-not-complete is a different claim from something-got-worse. The config
    # floor is one of them: a run that could not read part of its own input has
    # not verified the board, however clean the part it could read came out.
    #
    # An envelope whose schema version this build does not parse floors at 2 for the
    # same reason and not at 1: nothing was found to be worse, we were unable to
    # read the answer. Applied here rather than inside `DetectOutcome.exit_code`,
    # which returns 0 or 1 by contract -- `2` is the caller's to give.
    stage1 = EXIT_REGRESSION if current.regressions else EXIT_CLEAN
    schema_floor = EXIT_INCOMPLETE if outcome.schema_mismatch else EXIT_CLEAN
    return max(stage1, outcome.exit_code, unreadable_floor, schema_floor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bmc-sensor-audit",
        description="Find the sensors that should be reporting and are not.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    declare = subparsers.add_parser(
        "declare", help="read the configuration and report what it declares")
    declare.add_argument("--config", required=True, action="append",
                         help="entity-manager JSON file or directory (repeatable)")
    declare.set_defaults(func=_cmd_declare)

    coverage = subparsers.add_parser(
        "coverage", help="diff a declaration against what a machine reports")
    coverage.add_argument("--config", required=True, action="append",
                          help="entity-manager JSON file or directory (repeatable)")
    source = coverage.add_mutually_exclusive_group(required=True)
    source.add_argument("--target", help="Redfish base URL, e.g. https://bmc.example")
    source.add_argument("--walk", help="a recorded walk, instead of live hardware")
    coverage.add_argument("--username")
    coverage.add_argument("--password")
    coverage.add_argument("--insecure", action="store_true",
                          help="do not verify TLS; BMCs ship self-signed certificates")
    coverage.add_argument("--timeout", type=float, default=15.0)
    coverage.add_argument("--json", action="store_true", help="machine-readable output")
    coverage.add_argument("--include-disabled", action="store_true",
                          help="also expect sensors the config marks Status: disabled")
    coverage.set_defaults(func=_cmd_coverage)

    detect = subparsers.add_parser(
        "detect", help="coverage plus liveness, in one run and one exit code")
    detect.add_argument("--config", required=True, action="append",
                        help="entity-manager JSON file or directory (repeatable)")
    detect_source = detect.add_mutually_exclusive_group(required=True)
    detect_source.add_argument("--target", help="Redfish base URL")
    detect_source.add_argument("--walk", action="append",
                               help="a recorded walk; repeatable, OLDEST FIRST -- "
                                    "stuck-at needs history and one walk is one sample")
    detect.add_argument("--username")
    detect.add_argument("--password")
    detect.add_argument("--insecure", action="store_true",
                        help="do not verify TLS; BMCs ship self-signed certificates")
    detect.add_argument("--timeout", type=float, default=15.0)
    detect.add_argument("--include-disabled", action="store_true")
    detect.add_argument("--strict-declines", action="store_true",
                        help="fail on data-sufficiency and unrecognised declines too")
    detect.add_argument("--no-stuck-at", action="store_true",
                        help="do not expect readings to vary; turns off liveness")
    detect.add_argument("--supplemental",
                        help="operator declarations the configuration cannot make: "
                             "which sensors are redundant, which are counters")
    detect.add_argument("--model-out", help="write the generated domain model here")
    detect.add_argument("--manifest-out", help="write the generation manifest here")
    detect.add_argument("--attest-out",
                        help="write a per-run record of what was checked, what was "
                             "declined, and the measurements behind each finding")
    detect.set_defaults(func=_cmd_detect)

    capture = subparsers.add_parser(
        "capture", help="record a walk to disk, for a before/after gate")
    capture.add_argument("--target", required=True)
    capture.add_argument("--out", required=True, help="file to write")
    capture.add_argument("--username")
    capture.add_argument("--password")
    capture.add_argument("--insecure", action="store_true",
                         help="do not verify TLS; BMCs ship self-signed certificates")
    capture.add_argument("--timeout", type=float, default=15.0)
    capture.set_defaults(func=_cmd_capture)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
