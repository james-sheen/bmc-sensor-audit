"""Command line entry point for Stage 1.

    bmc-sensor-audit coverage   --config <path> --target https://<bmc> [--insecure]
    bmc-sensor-audit coverage   --config <path> --walk recorded-walk.json
    bmc-sensor-audit declare    --config <path>
    bmc-sensor-audit regression --before before.json --after after.json

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
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .verticals.field_strictness import strict_fields_as_text
from presence_audit import plugins as _plugins
from presence_audit import vocabulary as _vocabulary
from .verticals import bmc as _bundled
from presence_audit.vocabulary import PluginError
from .inventory.declaration_source import (DeclarationSourceError,
                                           candidate_from_walk,
                                           load_declaration_source, merge_sources)
from presence_audit import exit_contract as _exit_contract
from presence_audit.diff import compare
from .inventory.entity_manager import load_declaration
from .inventory.redfish import (CertificatePinError, RedfishClient, Walk,
                                order_walks, validate_walk,
                                etag_cache, membership_unchanged,
                                walk_chassis, walk_digest, walk_from_dict)
from presence_audit.regression import compare_walks, parse_prefix_map
from presence_audit.report import (as_json, as_text, regression_as_json, regression_as_text,
                     )

# DERIVED from the core's contract, not written out again here. The three
# numbers had two homes -- this line and `presence_audit.exit_contract` -- and
# two records of one fact drift. The NAMES stay this tool's own: `regression`
# is what a `1` means here, and the core cannot know that.
EXIT_CLEAN = _exit_contract.CLEAN
EXIT_REGRESSION = _exit_contract.FINDINGS
EXIT_INCOMPLETE = _exit_contract.INCOMPLETE


def _now() -> datetime:
    """The instant a resident cycle starts, and the one it stamps and files at.

    A module attribute rather than a bare call, together with `_sleep`, so a
    bench can drive a resident run through an hour of simulated collection in
    a second -- the only way a test can watch a forecast mature.
    """
    return datetime.now(timezone.utc)


_sleep = time.sleep


def _load_recorded_walk(path: str) -> Walk:
    """Rehydrate a walk from a recorded fixture.

    Recording once and diffing repeatedly is how the firmware-upgrade gate works:
    capture before, capture after, compare both against the config. It is also
    how the test suite runs with no hardware in the room.
    """
    return walk_from_dict(json.loads(Path(path).read_text()))


def _walk_span(walks: list[Walk]) -> str | None:
    """How much wall-clock time these walks cover, or nothing if it is unknowable.

    Nothing rather than zero when any walk is unstamped: a run whose captures carry
    no times covers an unknown span, and printing `0:00:00` would state the one
    answer that is certainly wrong.
    """
    if len(walks) < 2:
        return None
    stamps = [w.captured_at for w in walks]
    if not all(stamps):
        return None
    try:
        first = datetime.fromisoformat(stamps[0])
        last = datetime.fromisoformat(stamps[-1])
    except ValueError:
        return None
    return f"{last - first} ({stamps[0]} to {stamps[-1]})"


class CredentialError(Exception):
    """A credential this run was told to use and could not read.

    Caught in `main` rather than in each subcommand: the three that can reach a
    BMC fail this way identically, and a per-subcommand catch is three chances
    to forget one. **It is exit 2, not exit 1** -- a run that could not obtain a
    password did not audit a machine and find nothing.
    """


def _add_connection_flags(sub: argparse.ArgumentParser) -> None:
    """The flags every subcommand that can reach a BMC accepts.

    **One declaration, three subcommands.** These were three copies, which is how
    `--password-env` would have landed on `capture` and not on `coverage` -- and
    the operator who found the gap on one would have no reason to re-check the
    others.
    """
    sub.add_argument("--username")
    credential = sub.add_mutually_exclusive_group()
    credential.add_argument("--password",
                            help="DISCOURAGED: this crosses argv, where ps can "
                                 "read it on a shared host. Prefer --password-env")
    credential.add_argument("--password-env", metavar="NAME",
                            help="read the password from this environment "
                                 "variable, so the value never enters argv")
    credential.add_argument("--password-file", metavar="PATH",
                            help="read the password from the first line of this "
                                 "file, so the value never enters argv")
    sub.add_argument("--insecure", action="store_true",
                     help="do not verify TLS; BMCs ship self-signed certificates")
    sub.add_argument("--cafile", metavar="PATH",
                     help="verify the BMC against this certificate or CA bundle "
                          "instead of the system trust store")
    sub.add_argument("--pin-sha256", metavar="FINGERPRINT",
                     help="require the BMC to present exactly this certificate, "
                          "by SHA-256 of its DER. Replaces chain verification, "
                          "which a self-signed certificate cannot satisfy")
    sub.add_argument("--timeout", type=float, default=15.0)


def _resolve_password(args: argparse.Namespace) -> str | None:
    """The password, from whichever surface was named.

    **Read at the moment of use, and never echoed.** A missing environment
    variable or an unreadable file is a run that could not happen -- reported,
    not silently treated as *no password*, which would reach the BMC as an
    anonymous request and fail with a misleading 401.
    """
    if getattr(args, "password_env", None):
        value = os.environ.get(args.password_env)
        if value is None:
            raise CredentialError(f"--password-env names {args.password_env!r} and that "
                           f"variable is not set")
        return value
    if getattr(args, "password_file", None):
        try:
            first = Path(args.password_file).read_text(encoding="utf-8").split("\n", 1)[0]
        except OSError as exc:
            raise CredentialError(f"--password-file {args.password_file}: "
                           f"{exc.strerror or exc}") from exc
        # A trailing newline is what every editor adds and no BMC expects.
        return first.rstrip("\r")
    return getattr(args, "password", None)


def _client(args: argparse.Namespace) -> RedfishClient:
    return RedfishClient(args.target, username=args.username,
                         password=_resolve_password(args),
                         verify_tls=not args.insecure, timeout=args.timeout,
                         cafile=getattr(args, "cafile", None),
                         pin_sha256=getattr(args, "pin_sha256", None))


#: The one line of `capture` output that is a CONTRACT rather than prose.
#:
#: **Reported from outside (issue #6), against a fix from an hour earlier.** A
#: skip and a walk both exit `0` -- correctly, because a skip is clean, and a
#: fourth exit code would break the three-valued vocabulary every tool in this
#: family shares. So the only difference was a printed sentence, and a consumer
#: had to match prose that nothing promised to keep.
#:
#: This is the promise instead: `capture` always prints exactly one `OUTCOME `
#: line, its value is one of `OUTCOMES`, and both are covered by the same
#: stability statement as `walk/1`. Everything else `capture` prints is prose
#: and may be reworded at any time.
OUTCOME = "OUTCOME "
OUTCOMES = ("walked", "unchanged")


def _cmd_capture(args: argparse.Namespace) -> int:
    """Record a walk to disk, for diffing later or for a before/after gate."""
    client = _client(args)

    cache_path = Path(args.etag_cache) if args.etag_cache else None
    if cache_path is not None and cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            # Reported, and then the walk happens anyway. An unreadable cache is
            # a reason to do the full work, never a reason to skip it.
            print(f"  etag cache unusable ({error}); walking in full")
            cache = None
        if cache is not None:
            verdict, why = membership_unchanged(client, cache)
            if verdict is True:
                print(f"sensor set unchanged since {cache.get('captured_at')} "
                      f"-- {why}")
                print(f"  {args.out} left as it was; {len(cache.get('collections') or {})} "
                      f"request(s) instead of a full walk")
                # **Says what it did NOT check.** Collection ETags answer
                # membership. A threshold edited on a sensor that stayed present
                # moves that sensor's resource and not its collection.
                print("  this answers membership only: a threshold or unit "
                      "changed on a sensor that stayed present would not show "
                      "here. Drop --etag-cache to compare configuration")
                print(f"{OUTCOME}unchanged")
                return EXIT_CLEAN
            print(f"  {why}; walking in full")

    walk = walk_chassis(client)
    text = json.dumps(walk.to_dict(), indent=2)
    Path(args.out).write_text(text)
    print(f"{OUTCOME}walked")
    print(f"wrote {len(walk)} sensor(s) to {args.out}")
    if args.print_digest:
        # The whole of the fleet handle, and deliberately the whole of it. The
        # collector wraps `{unit_key, digest, walk_ref}` in its own envelope, on its
        # own side of the identity line; this tool prints which capture and never
        # learns which machine.
        print(f"  digest      {walk_digest(text)}")
    if cache_path is not None:
        fresh = etag_cache(client)
        cache_path.write_text(json.dumps(fresh, indent=2) + "\n")
        found = len(fresh["collections"])
        print(f"  etag cache  {found} collection(s) -> {cache_path}"
              if found else
              f"  etag cache  this BMC returned no ETags; {cache_path} cannot "
              f"shorten the next walk")
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
    drifting = [s for s in walk if s.undeclared]
    if drifting:
        # Surfaced at capture time without a flag, because this is where the
        # evidence is. The capture keeps the property names, so the detail is
        # recoverable later -- but a signal nobody knows to ask for is one nobody
        # asks for.
        print(f"  {len(drifting)} sensor(s) carry properties the published schema "
              f"does not declare")
        print("    coverage --strict-fields names them")
    if not walk.complete:
        # Written anyway: a partial capture is still evidence, and deleting it
        # loses the record of WHICH subtree failed. But it must not be mistaken
        # for a baseline, and a diff against it withholds absence findings.
        print(f"  ** INCOMPLETE -- {len(walk.errors)} fetch(es) failed **")
        for path, reason in walk.errors[:5]:
            print(f"     {path}: {reason}")
        return EXIT_INCOMPLETE
    return EXIT_CLEAN


def _cmd_declare_candidate(args: argparse.Namespace) -> int:
    """Derive a `pdr/1` CANDIDATE from a walk, which asserts nothing.

    **The circularity hazard is the founding problem of this tool, one door over.**
    A declaration derived from a walk of an unprovisioned board is an empty
    declaration that reads healthy, and nothing inside the file can tell that from a
    good one. So what is written here carries `reviewed: null` and is refused by
    `coverage` and `detect` until a person adds their name.

    `--candidate` is required rather than implied. The flag is the operator saying
    they know what this produces, and a command that silently emitted an
    assert-nothing file would eventually be read as one that asserts something.
    """
    # argparse cannot express *required with this other flag*, so it is checked
    # here. Named one at a time rather than as one message about three flags: an
    # error listing everything that could be wrong is one nobody reads to the end.
    for flag, value, why in (
            ("--candidate", args.candidate,
             "what this writes asserts nothing, and the flag is you saying so"),
            ("--out", args.out, "there is nowhere to write it"),
            ("--platform", args.platform,
             "a declaration scoped to nothing in particular is one nobody can tell "
             "was pointed at the wrong machine")):
        if not value:
            print(f"--from-walk needs {flag}: {why}", file=sys.stderr)
            return EXIT_INCOMPLETE

    walk = _load_recorded_walk(args.from_walk)
    try:
        payload = candidate_from_walk(walk, platform=args.platform,
                                      firmware=args.firmware,
                                      source_path=args.from_walk)
    except DeclarationSourceError as error:
        print(f"{args.from_walk}: {error}", file=sys.stderr)
        return EXIT_INCOMPLETE

    Path(args.out).write_text(json.dumps(payload, indent=2))
    print(f"wrote a {payload['format']} CANDIDATE to {args.out}")
    print(f"  platform     {payload['platform']}")
    print(f"  firmware     {payload['firmware'] or '(unstated)'}")
    print(f"  sensors      {len(payload['sensors'])}")
    print("  reviewed     null")
    print("\nThis file asserts nothing and will be REFUSED by coverage and detect.")
    print("Read it against the platform's documentation, then add:")
    print('    "reviewed": {"by": "<name>", "on": "<date>"}')
    return EXIT_CLEAN


def _cmd_declare(args: argparse.Namespace) -> int:
    if args.from_walk:
        return _cmd_declare_candidate(args)

    declaration = load_declaration(args.config)
    declaration, refusal = _with_declaration_sources(declaration, args.declaration)
    if refusal:
        print(refusal, file=sys.stderr)
        return EXIT_INCOMPLETE
    for source in declaration.sources:
        # Printed here too, so an operator can check a declaration file loads and
        # see what it claims BEFORE pointing a gate at it.
        print(source.provenance_line())
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


def _with_declaration_sources(declaration, paths):
    """Layer any `--declaration` files under the manufacturer's declaration.

    Returns `(declaration, None)` or `(None, message)`. A refusal stops the run
    rather than degrading it, for the same reason a bad supplemental file does: a
    source that half-loaded would produce a report whose clean rows and absent rows
    came from different populations, and nothing downstream could tell which.

    **The candidate refusal arrives here**, before a single sensor is compared, so
    an unreviewed declaration can never contribute a row to any report.
    """
    if not paths:
        return declaration, None
    sources = []
    for path in paths:
        try:
            sources.append(load_declaration_source(path))
        except DeclarationSourceError as error:
            return None, str(error)
    return merge_sources(declaration, sources), None


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


def _report_unobserved_fields(walk: Walk, requested: bool) -> int:
    """A strictness check that was asked for and could not run floors the exit at 2.

    Returns the floor, the same shape as `_report_unreadable`, so a caller composes
    it with whatever else it found instead of choosing between them.

    **Reported from outside, and it sat on the thesis.** The report printed
    `NOT CHECKED` and the process exited 0, so a pipeline gating on
    `--strict-fields` over a capture written before object properties were
    recorded went green with the strictness half never having run. Honest prose
    beside a clean exit code is the exact failure this tool is pointed at: the
    exit code is the claim a gate reads, and the prose is not.

    The precedent is this repository's own, in three places already -- `detect`
    without the engine prints its coverage findings and exits 2, an unreadable
    configuration file floors at 2, and an incomplete walk exits 2. All three are
    the same sentence: a run that could not complete the audit it was asked for
    must not read as clean.

    **Only when the check was requested.** An old capture used without the flag is
    a perfectly complete coverage run, and flooring it would fail every gate that
    never asked the question.

    **And only for the requested check.** `regression` computes field drift
    opportunistically when both walks happen to carry observations, says so when
    they do not, and does NOT floor: the removal, rename and threshold comparisons
    it was actually asked for all completed. Flooring there would turn a fully
    answered question red because a bonus one could not be asked, which is how a
    gate teaches people to stop reading it.
    """
    if not requested or walk.fields_observed:
        return EXIT_CLEAN
    from .verticals.field_strictness import unobserved_reason

    print(f"\nfield strictness was requested and could not be checked: "
          f"{unobserved_reason(walk)}.\nThis run has not answered the question it "
          f"was asked, so it does not exit clean.", file=sys.stderr)
    return EXIT_INCOMPLETE


def _report_uncomparable_fields(before: Walk, after: Walk, requested: bool) -> int:
    """The same rule, applied to the comparison rather than to one walk.

    Field drift is computed opportunistically when both walks happen to carry
    observations, and `regression` reports honestly when they do not -- but until
    there was a flag, that was ALL it could do. A pipeline that gates firmware on
    `regression` and needs drift covered had no handle: the run said *not
    computed* in prose and exited on the strength of the comparisons that did run.
    The same could-not-complete-reads-as-clean shape as the strictness finding,
    one door over, and reported from outside in the same way.

    **A flag rather than a default, and the weight is the reason.** Flooring
    flagless would turn every regression run against an older baseline into exit
    2, breaking the removal and rename gating that works perfectly well on those
    captures -- a real cost paid by exactly the operators the subcommand serves
    best. So drift stays best-effort until somebody asks for it, and asking is
    what makes the existing rule apply.

    **Which side is named**, because the fix differs: one old capture means
    re-capture that one, and two mean the baseline predates the field entirely.
    """
    if not requested or (before.fields_observed and after.fields_observed):
        return EXIT_CLEAN
    from .verticals.field_strictness import unobserved_reason

    missing = [(label, walk) for label, walk in (("--before", before), ("--after", after))
               if not walk.fields_observed]
    which = ("neither capture carries a record" if len(missing) == 2
             else "one of the two captures carries no record")
    print(f"\nfield drift was requested and could not be compared: {which} of what "
          f"properties each object reported.", file=sys.stderr)
    for label, walk in missing:
        print(f"    {label}: {unobserved_reason(walk)}", file=sys.stderr)
    print("This run has not answered the question it was asked, so it does not "
          "exit clean.", file=sys.stderr)
    return EXIT_INCOMPLETE


def _cmd_coverage(args: argparse.Namespace) -> int:
    declaration = load_declaration(args.config)
    declaration, refusal = _with_declaration_sources(declaration, args.declaration)
    if refusal:
        print(refusal, file=sys.stderr)
        return EXIT_INCOMPLETE
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
    rendered = (as_json(report, target=target,
                        walk=walk if args.strict_fields else None) if args.json
                else as_text(report, target=target))
    print(rendered)

    if args.strict_fields and not args.json:
        # What it FINDS is reported and never scored. A vendor extension is not a
        # regression -- the firmware is doing something the standard permits, and
        # a gate that failed on the first one gets switched off within a week,
        # taking the signal with it. What DOES fail a gate is an extension that
        # ARRIVED, which is a comparison between two firmware versions and belongs
        # to `regression`.
        #
        # Whether it RAN is a different question, and it is scored. See below.
        print(strict_fields_as_text(walk, target=target))
    strict_floor = _report_unobserved_fields(walk, args.strict_fields)

    if not report.walk_complete:
        return EXIT_INCOMPLETE
    # Composed the way `detect` composes its two stages: the worse wins, and 2 outranks
    # 1 because could-not-read is a different claim from something-got-worse.
    stage1 = EXIT_REGRESSION if report.regressions else EXIT_CLEAN
    return _exit_contract.compose(stage1, unreadable_floor, strict_floor)


def _cmd_detect(args: argparse.Namespace) -> int:
    """Both stages in one run, and one exit code.

    Stage 1 answers presence; Stage 2 answers liveness for what is present. They are
    composed rather than merged, because they fail differently: a walk that could not
    complete is not a board with missing sensors, and neither is an engine that is not
    installed.
    """
    stores_refusal = _stores_refusal(args)
    if stores_refusal:
        print(stores_refusal, file=sys.stderr)
        return EXIT_INCOMPLETE
    declaration = load_declaration(args.config)
    declaration, refusal = _with_declaration_sources(declaration, args.declaration)
    if refusal:
        print(refusal, file=sys.stderr)
        return EXIT_INCOMPLETE
    if not declaration.sensors and not declaration.unreadable:
        print("no sensors declared by any file under the given paths", file=sys.stderr)
        return EXIT_INCOMPLETE
    unreadable_floor = _report_unreadable(declaration)

    # `--walk` is repeatable and CHRONOLOGICAL, oldest first: stuck-at needs history,
    # and one walk is one sample. A live target gives exactly one.
    if args.resident:
        # The resident loop walks for itself, every cycle; a first walk here
        # would be one sample no cycle ever fed.
        walks, target = [], args.target
    elif args.walk:
        walks = [_load_recorded_walk(path) for path in args.walk]
        # The last walk supplies every current reading, so the order is not a
        # presentation detail. A shell glob hands over lexical order, in which
        # `walk10` precedes `walk9`.
        walks, ordering = order_walks(walks)
        if ordering:
            print(f"\n{ordering}", file=sys.stderr)
        span = _walk_span(walks)
        if span:
            # The verdict is over the values; this is over the clock. The engine is
            # told every sample is a minute old regardless of when the walk was
            # taken, so `frozen` alone does not say whether the reading held still
            # for a minute or for a shift. That distinction is only in the stamps.
            print(f"\n{len(walks)} walks covering {span}")
        target = args.walk[-1]
    else:
        walks = [walk_chassis(_client(args))]
        target = args.target

    reports = [compare(declaration, walk,
                       include_disabled_in_config=args.include_disabled)
               for walk in walks]
    current = reports[-1] if reports else None
    if current is not None:
        print(as_text(current, target=target))

        if not current.walk_complete:
            # An incomplete walk is not an empty machine, and it is not a model
            # worth feeding either. Stop before the engine sees a partial picture.
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

    from presence_audit.feeder import evaluate, feed
    from presence_audit.generator import DEFAULT_SAMPLE_INTERVAL_S, generate
    from presence_audit.supplemental import (SupplementalError, load_supplemental,
                                      unmatched_names)
    from presence_audit.report import detect_as_text, supplemental_as_text

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
        # Printed whether or not anything is missing a number, so the reader sees
        # what was declared before they read a verdict that rests on it.
        print(supplemental_as_text(supplemental))

    # The domain id is DECLARED here rather than defaulted in the generator.
    # It used to default to this distribution's name, in code that is now
    # domain-neutral and cannot know what domain it is generating for. The
    # string is unchanged, so an emitted model is byte-identical to before.
    model, manifest = generate(declaration, domain_id="bmc-sensor-audit",
                               expect_variation=not args.no_stuck_at,
                               supplemental=supplemental)
    if args.model_out:
        Path(args.model_out).write_text(yaml.safe_dump(model))
    if args.manifest_out:
        Path(args.manifest_out).write_text(json.dumps(manifest.to_dict(), indent=2))

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        handle.write(yaml.safe_dump(model))
        model_path = handle.name
    # DURABLE STORES when the operator asks for them. The engine has kept a
    # durable prediction ledger since 0.2.3 and `SqlitePredictionLedger` became
    # a supported top-level name in 0.2.6. Handing the session one was half of
    # it: nothing here FILED a prediction, so the file stayed empty however many
    # horizons passed. `_file_forecasts` below is the other half, and
    # `--history` is the third, because grading needs the readings too.
    #
    # NO EXTRA IS DECLARED FOR THIS, and that is measured rather than assumed:
    # both stores import nothing outside the standard library, and the engine
    # itself publishes no `resident` extra. An extra that installs nothing is
    # ceremony, and a caller who pip-installed it would be told they had gained
    # a capability they already had.
    try:
        ledger, history = _open_stores(args)
    except _StoresUnavailable as error:
        print(f"\n{error}", file=sys.stderr)
        return EXIT_INCOMPLETE
    # The collector's cadence is the file's to declare; `--every` is how often a
    # resident run actually walks, and they are allowed to differ only openly.
    declared = float(manifest.sampling_interval_s or DEFAULT_SAMPLE_INTERVAL_S)
    cadence = float(args.every) if args.every is not None else declared
    if cadence != declared:
        print(f"\n--every {cadence:g}s differs from the declared cadence of "
              f"{declared:g}s; the model counts its windows in declared steps, "
              f"so a sample every {cadence:g}s fills them at a different rate",
              file=sys.stderr)
    horizon = float(args.horizon) if args.horizon is not None else cadence
    if args.resident:
        return _resident(args, declaration, model_path, manifest, cadence,
                         horizon, unreadable_floor, ledger, history)
    session = EngineSession(history=history, ledger=ledger)
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

        from presence_audit.attestation import build_attestation
        # The artifact leaves through a different door from every committed file,
        # and the hygiene perimeter guards commits. `target` is a Redfish URL by
        # default, so an artifact uploaded from CI can publish an internal hostname
        # in a channel no hook ever scans. The label is the operator's override; no
        # guessing at which hostnames look internal happens here, because that is
        # pattern-matching a judgement only they can make.
        artifact = build_attestation(session, envelope, described, manifest,
                                     target=args.attest_target_label or target,
                                     attest_fn=attest)
        Path(args.attest_out).write_text(json.dumps(artifact, indent=2))

        # Said on the terminal, not only inside the file. The artifact already
        # accounts for this honestly -- `unattested` is a required field and the
        # shipped validator reads it -- but an operator who asked for evidence and
        # received an artifact carrying none finds that out only by opening it.
        # A quiet gap is not a false claim, and it is still a gap nobody sees.
        #
        # No exit floor: `check` completed and its findings stand. What did not
        # complete is the evidence the engine attaches to them, which is a weaker
        # thing than the audit itself.
        from presence_audit.report import unattested_notice

        notice = unattested_notice(artifact, args.attest_out)
        if notice:
            print(f"\n{notice}", file=sys.stderr)
    print(detect_as_text(outcome, feed_result))

    if ledger is not None:
        if args.walk:
            # A recorded reading has no future anyone will read it against, so a
            # forecast from it could only ever mature `ungradeable`.
            print("\nledger: nothing filed -- recorded walks have no future a "
                  "later run could read a forecast against", file=sys.stderr)
        else:
            issued, rolled, withheld = _file_forecasts(session, manifest,
                                                       horizon, cadence)
            for reason in withheld:
                print(f"coupling filed nothing -- {reason}", file=sys.stderr)
            print(f"\nfiled {issued} forecast(s)"
                  + (f" and {rolled} coupling value(s)" if rolled else "")
                  + f", {horizon:g}s ahead, into {args.ledger}")
            print(_ledger_line(ledger))
            if history is None:
                print("a later run can grade these only against readings taken "
                      "near the moment they mature, and this run's readings end "
                      "with it: pass --history PATH, or run --resident",
                      file=sys.stderr)

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
    return _exit_contract.compose(stage1, outcome.exit_code,
                                  unreadable_floor, schema_floor)


def _stores_refusal(args: argparse.Namespace) -> str | None:
    """A flag combination that would do nothing, or record something false.

    Refused rather than tolerated, on the family's rule: a flag that changes
    nothing reads as a flag that worked.
    """
    if getattr(args, "history", None) and args.walk:
        return ("--history keeps readings for a later run to grade forecasts "
                "against, and a recorded walk has no instant of its own: its "
                "readings are stamped in a ladder ending NOW, so writing them to "
                "a durable store would put invented times into a record another "
                "run trusts. Use --history with --target")
    if args.resident and not args.target:
        return ("--resident walks the same live target again and again; it "
                "needs --target. Recorded walks already hold their whole history")
    if args.resident and not getattr(args, "ledger", None):
        return ("--resident exists to file forecasts and grade them as they "
                "mature; without --ledger they would die with the process. "
                "Pass --ledger PATH")
    if not args.resident and (args.every is not None or args.cycles is not None):
        return ("--every and --cycles set a resident run's cadence and length, "
                "and this run is not resident; pass --resident, or drop them")
    if not args.resident and getattr(args, "keep_walks", None):
        return ("--keep-walks keeps the walks a resident run takes, and this run "
                "is not resident; one walk is written with `capture --out`")
    return None


def _open_stores(args: argparse.Namespace) -> tuple[Any, Any]:
    """The ledger a run files into and the history it grades against.

    TWO STORES, BECAUSE GRADING NEEDS BOTH. A forecast is scored against the
    reading nearest `predicted_at + horizon`, inside a grace window. A single
    walk holds one reading, stamped at NOW -- which is after the window of
    every forecast an earlier run filed -- so a durable ledger alone grades
    everything `ungradeable`. The readings have to persist as well, and
    `SqliteObservationHistory` is the engine's supported name for that.
    """
    ledger = history = None
    if getattr(args, "ledger", None):
        from arbiter_engine import SqlitePredictionLedger

        ledger = SqlitePredictionLedger(args.ledger)
    if getattr(args, "history", None):
        from arbiter_engine import SqliteObservationHistory

        if not hasattr(SqliteObservationHistory, "series_keys"):
            # Before arbiter-engine 0.2.9 the durable store could not list its
            # series, and describing a session built over it RAISED. Refused by
            # name here rather than left to fail three calls later.
            raise _StoresUnavailable(
                "--history needs an engine whose durable store can list its "
                "series, which arrived in arbiter-engine 0.2.9; "
                "pip install --upgrade 'bmc-sensor-audit[detect]'")
        history = SqliteObservationHistory(args.history)
    return ledger, history


class _StoresUnavailable(RuntimeError):
    """The installed engine cannot hold what the flags asked it to."""


#: The rollout's refusals that explain why a declared coupling filed NOTHING.
#: Surfaced by name, because a run that files forecasts for the driver and none
#: for the coupling reads as a coupling that is being graded when it is not.
_COUPLING_FILING_REFUSALS = ("gain_not_adopted", "no_declared_tolerance")


def _file_forecasts(session: Any, manifest: Any, horizon_s: float,
                    step_s: float) -> tuple[int, int, list[str]]:
    """File what this cycle predicts, so a later one has something to grade.

    THE LEDGER WAS WIRED AND NOTHING FLOWED INTO IT. `detect` called `check`
    and `model_describe`, and neither files a prediction -- measured, forty
    walks through `--ledger` left zero rows. So `calibration()` could not be
    non-null here however many horizons passed.

    Two sources, and ONLY what the model declares. `project` files the
    engine's own forecast of every indicator that declares dynamics, with a
    random walk beside it as the yardstick; a coupling's driver carries one,
    and nothing else here does. `rollout` files what a DECLARED coupling
    predicts downstream, and only once its gain has been written down -- with
    the gain withheld it declines by name and files nothing. No projector is
    put on a sensor the declaration did not give one: that would grade a model
    nobody chose.

    THE ROLLOUT IS SEEDED FROM THE DRIVER'S FORECAST, AND UNTIL 0.3.5 IT WAS
    NOT. Seeded from the current readings with no action scheduled, a rollout
    moves nothing: every value is held at its reading, nothing any coupling
    predicts is in it, and the engine declined all of it
    `no_declared_tolerance` -- whose remedy, a declared spread, cannot help a
    value nothing moved. Measured on a bench: an adopted gain filed nothing
    for sixteen cycles WITH a spread declared. Seeded from the forecast, the
    driver moves, the coupling carries the move downstream, and the forecast's
    own band passes through the gain -- so an adopted gain is graded with or
    without a declared spread, which only widens that band by the gain's own
    doubt.
    """
    from arbiter_engine.api import project, rollout

    projection = project(session, horizon_s=horizon_s).to_dict()
    issued = int(((projection.get("projection") or {}).get("checked") or {})
                 .get("forecasts_issued", 0) or 0)
    rolled, withheld = 0, []
    if getattr(manifest, "coupled", None):
        simulation = (rollout(session, horizon_s=horizon_s, step_s=step_s,
                              seed_mode="projected",
                              file_predictions=True).to_dict()
                      .get("simulation") or {})
        rolled = int((simulation.get("checked") or {})
                     .get("predictions_filed", 0) or 0)
        # WHY A COUPLING FILED NOTHING, in the engine's words, or it reads as
        # a coupling being graded.
        withheld = sorted({f"{d['reason']}: {d.get('detail', '')}"
                           for d in simulation.get("not_checked") or []
                           if d.get("reason") in _COUPLING_FILING_REFUSALS})
    return issued, rolled, withheld


def _ledger_line(ledger: Any) -> str:
    """The ledger's own figures, with their denominators, in one line.

    A rate is printed only once something has been graded -- before that the
    honest figure is NONE, and a zero would read as a measurement.
    """
    cal = ledger.calibration()
    graded = int(cal.get("confirmed", 0)) + int(cal.get("falsified", 0))
    parts = [f"ledger: {cal.get('recorded', 0)} recorded, "
             f"{cal.get('pending', 0)} pending, {graded} graded"]
    if cal.get("ungradeable"):
        parts.append(f"{cal['ungradeable']} ungradeable")
    if graded:
        parts.append(f"confirm_rate {cal['confirm_rate']:.2f} against an "
                     f"expected {cal['expected_confirm_rate']:.2f}")
    else:
        parts.append("confirm_rate none yet -- nothing has matured")
    for model_id, row in sorted((cal.get("by_model") or {}).items()):
        parts.append(f"crps {model_id} {row['crps_approx']:.4g} (n={row['n']})")
    own = cal.get("own_projections") or {}
    if own.get("n"):
        parts.append(f"coupling projections crps {own['crps_approx']:.4g} "
                     f"(n={own['n']})")
    return "; ".join(parts)


def _resident(args: argparse.Namespace, declaration: Any, model_path: str,
              manifest: Any, cadence: float, horizon: float,
              unreadable_floor: int, ledger: Any, history: Any) -> int:
    """Walk one live target again and again, filing and grading as it goes.

    E2 of the 0.2.7 verification asked for exactly this: a calibration figure
    that no human had to re-run a command to produce. Each cycle opens a fresh
    engine session over the SAME two stores, feeds this walk's readings,
    checks -- which grades every forecast that has matured since -- and files
    the next ones. So the first figure appears one horizon plus the ledger's
    grace after the fifth reading, which is the fewest a forecast can be
    fitted on.

    Each cycle pins the engine's clock to its own start, so the readings it
    feeds and the forecasts it files carry one instant between them.
    """
    from arbiter_engine.api import EngineSession, as_of, check, model_describe

    from presence_audit.feeder import evaluate, feed
    from presence_audit.report import detect_as_text

    if history is None:
        from arbiter_engine import InMemoryObservationHistory

        history = InMemoryObservationHistory()
    print(f"\nresident: walking {args.target} every {cadence:g}s and filing "
          f"forecasts {horizon:g}s ahead into {args.ledger}"
          + (f", for {args.cycles} cycle(s)" if args.cycles else
             ", until interrupted"), file=sys.stderr)
    # THE RAW WALKS, KEPT. The ledger holds what was forecast and the history
    # what was read, and neither is what `adopt` fits from: it takes walks. So
    # without these a resident run could grade a board for a day and leave
    # nothing to adopt a gain from -- and nothing anyone else could re-derive
    # its figures from.
    kept = None
    if getattr(args, "keep_walks", None):
        kept = Path(args.keep_walks)
        kept.mkdir(parents=True, exist_ok=True)
        print(f"resident: keeping each cycle's walk in {kept}", file=sys.stderr)
    client = _client(args)
    worst: int | None = None
    said_withheld: list[str] = []
    cycle = 0
    try:
        while args.cycles is None or cycle < args.cycles:
            cycle += 1
            at = _now()
            with as_of(at):
                walk = walk_chassis(client)
                if kept is not None:
                    # BEFORE it is judged, so a cycle that fails to complete
                    # still leaves the raw walk that explains why.
                    (kept / f"walk-{at:%Y%m%dT%H%M%SZ}.json").write_text(
                        json.dumps(walk.to_dict(), indent=2))
                report = compare(declaration, walk,
                                 include_disabled_in_config=args.include_disabled)
                if not report.walk_complete:
                    code = EXIT_INCOMPLETE
                    summary = "walk incomplete; nothing fed, nothing filed"
                else:
                    session = EngineSession(history=history, ledger=ledger)
                    session.load_model(model_path)
                    feed_result = feed(session, manifest, [report])
                    envelope = check(session).to_dict()
                    described = model_describe(session).to_dict()
                    outcome = evaluate(envelope, described, manifest,
                                       strict_declines=args.strict_declines,
                                       feed_result=feed_result)
                    if cycle == 1:
                        print(as_text(report, target=args.target))
                        print(detect_as_text(outcome, feed_result))
                    issued, rolled, withheld = _file_forecasts(
                        session, manifest, horizon, cadence)
                    if withheld and withheld != said_withheld:
                        for reason in withheld:
                            print(f"coupling filed nothing -- {reason}",
                                  file=sys.stderr)
                        said_withheld = withheld
                    stage1 = EXIT_REGRESSION if report.regressions else EXIT_CLEAN
                    schema_floor = (EXIT_INCOMPLETE if outcome.schema_mismatch
                                    else EXIT_CLEAN)
                    code = _exit_contract.compose(stage1, outcome.exit_code,
                                                  unreadable_floor, schema_floor)
                    summary = (f"{len(report.regressions)} regression(s), "
                               f"{len(outcome.findings)} finding(s); filed "
                               f"{issued} forecast(s)"
                               + (f" and {rolled} coupling value(s)"
                                  if rolled else ""))
                line = _ledger_line(ledger)
            print(f"cycle {cycle} at {at:%Y-%m-%dT%H:%M:%SZ}: exit {code} -- "
                  f"{summary}; {line}")
            worst = code if worst is None else _exit_contract.compose(worst, code)
            if args.cycles is None or cycle < args.cycles:
                _sleep(cadence)
    except KeyboardInterrupt:
        print(f"\nresident: stopped after {cycle} cycle(s)", file=sys.stderr)
    # No cycle completed is not a clean board: nothing was verified.
    return EXIT_INCOMPLETE if worst is None else worst


def _cmd_adopt(args: argparse.Namespace) -> int:
    """Write one fitted gain into the supplemental file, or say why not.

    Runs the same pipeline `detect` runs, from the same inputs, rather than
    reading a number off an earlier report. Adopting is the one act here that
    changes what a later audit asserts, and it should not be possible to do it
    against a printout of a file that has since been edited. The cost is
    passing the walks twice, which is what `detect` already asks for.
    """
    from . import adopt as _adopt

    declaration = load_declaration(args.config)
    declaration, refusal = _with_declaration_sources(declaration, args.declaration)
    if refusal:
        print(refusal, file=sys.stderr)
        return EXIT_INCOMPLETE
    if not declaration.sensors:
        print("no sensors declared by any file under the given paths", file=sys.stderr)
        return EXIT_INCOMPLETE
    # THE SAME FLOOR `coverage` AND `detect` APPLY, and it was missing from the
    # first draft of this command -- caught by the derived guard table, which
    # parametrises over every `--config` subcommand precisely so a new one
    # cannot quietly skip it. A directory with one unparseable file returned 0
    # here: an unreadable configuration is not a clean board, it is an unknown
    # one, and adopting a number measured against part of a machine is worse
    # than not adopting it.
    unreadable_floor = _report_unreadable(declaration)

    try:
        import yaml
        from arbiter_engine.api import EngineSession, model_describe
    except ImportError as error:
        print(f"adopt needs the optional extra, which is not installed: {error}\n"
              "    pip install 'bmc-sensor-audit[detect]'", file=sys.stderr)
        return _exit_contract.compose(EXIT_INCOMPLETE, unreadable_floor)

    from presence_audit.feeder import feed
    from presence_audit.generator import generate
    from presence_audit.supplemental import (SupplementalError, load_supplemental,
                                             unmatched_names)

    try:
        supplemental = load_supplemental(args.supplemental)
    except SupplementalError as error:
        print(str(error), file=sys.stderr)
        return _exit_contract.compose(EXIT_INCOMPLETE, unreadable_floor)
    if not supplemental.couplings:
        print(f"{args.supplemental} declares no couplings, so there is nothing "
              f"to fit.", file=sys.stderr)
        # 2 only when a proposal was NAMED, because then the thing asked for
        # cannot exist. Asking what was fitted and being told nothing is an
        # answer, not a failure -- the same line this package already draws for
        # liveness warming up.
        return _exit_contract.compose(
            EXIT_INCOMPLETE if args.proposal else EXIT_CLEAN,
            unreadable_floor)
    missing = unmatched_names(supplemental,
                              {s.display_name for s in declaration.sensors})
    if missing:
        print(f"{args.supplemental} names {len(missing)} sensor(s) this "
              f"configuration does not declare:", file=sys.stderr)
        for name in missing:
            print(f"    {name}", file=sys.stderr)
        return _exit_contract.compose(EXIT_INCOMPLETE, unreadable_floor)

    walks = [_load_recorded_walk(path) for path in args.walk]
    walks, ordering = order_walks(walks)
    if ordering:
        print(f"{ordering}", file=sys.stderr)
    reports = [compare(declaration, walk,
                       include_disabled_in_config=args.include_disabled)
               for walk in walks]
    if not reports[-1].walk_complete:
        # The same floor `detect` applies, and for a stronger reason here: an
        # incomplete walk is a partial series, and a gain fitted on one is a
        # number about a window that did not happen.
        print("walk incomplete; nothing fitted", file=sys.stderr)
        return _exit_contract.compose(EXIT_INCOMPLETE, unreadable_floor)

    model, manifest = generate(declaration, domain_id="bmc-sensor-audit",
                               expect_variation=not args.no_stuck_at,
                               supplemental=supplemental)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        handle.write(yaml.safe_dump(model))
        model_path = handle.name
    session = EngineSession()
    session.load_model(model_path)
    feed_result = feed(session, manifest, reports)

    corpus = None
    if args.surprises:
        from arbiter_engine.surprises import load_surprises

        corpus, declines = load_surprises(args.surprises)
        if declines:
            # Refused, not warned. A corpus with unread keys scores a different
            # set of entries from the one the operator wrote, and the replay
            # gate below is the whole reason this command has a corpus at all.
            print(f"{args.surprises} carries keys this engine does not read:",
                  file=sys.stderr)
            for decline in declines:
                print(f"    {decline}", file=sys.stderr)
            return _exit_contract.compose(EXIT_INCOMPLETE, unreadable_floor)

    described = model_describe(session, corpus).to_dict()
    candidates = _adopt.proposals(described, manifest,
                                  grid_seconds=feed_result.interval_seconds)

    if feed_result.couplings_not_fed:
        print(f"{len(feed_result.couplings_not_fed)} declared coupling(s) did "
              f"not reach the model this run:", file=sys.stderr)
        for entry in feed_result.couplings_not_fed:
            print(f"    {entry['from']} -> {entry['to']}: "
                  f"{', '.join(entry['missing'])} not reading", file=sys.stderr)

    if args.list or not args.proposal:
        # An inspection, and it does not fail. A run that fitted nothing has not
        # gone wrong: a gain needs both ends reading across enough paired
        # changes, and not having them yet is the same fact as liveness warming
        # up, which this package reports at 0 rather than at 2.
        print(_render_proposals(candidates, feed_result))
        return _exit_contract.compose(EXIT_CLEAN, unreadable_floor)

    try:
        proposal = _adopt.find(candidates, args.proposal)
        stamp = _adopt.check(proposal, force=args.force)
        when = _adopt.now()
        basis = _adopt.basis_for(proposal, when=when, stamp=stamp)
        spread_basis = (_adopt.spread_basis_for(proposal, when=when)
                        if proposal.spread is not None else None)
        if args.dry_run:
            print(f"would set gain {proposal.gain:.6g} on {proposal.id}")
            print(f"would set gain_basis:\n    {basis}")
            if spread_basis is not None:
                print(f"would set gain_sigma {proposal.spread:.6g}")
                print(f"would set gain_sigma_basis:\n    {spread_basis}")
                raise_to = _adopt.format_for_spread(
                    _adopt.declared_format(args.supplemental))
                if raise_to is not None:
                    print(f"would raise the format to {raise_to!r}, the "
                          f"oldest that carries a spread")
            else:
                print(_no_spread(proposal), file=sys.stderr)
            print(f"\n{args.supplemental} is unchanged (--dry-run).")
            return _exit_contract.compose(EXIT_CLEAN, unreadable_floor)
        written = _adopt.write(args.supplemental, proposal, basis, spread_basis)
    except _adopt.AdoptionRefused as error:
        print(str(error), file=sys.stderr)
        return _exit_contract.compose(EXIT_INCOMPLETE, unreadable_floor)

    print(f"{args.supplemental}: {proposal.id} now declares gain "
          f"{proposal.gain:.6g}"
          + (f" with gain_sigma {written.spread:.6g}"
             if written.spread is not None else ""))
    print(f"  {basis}")
    if written.spread is not None:
        print(f"  {spread_basis}")
    if written.format_raised_to is not None:
        print(f"\nThe format was raised from {written.format_raised_from!r} to "
              f"{written.format_raised_to!r}, the oldest that carries a "
              f"spread: a build reading only the older one refuses this file "
              f"by name rather than dropping the spread.")
    if written.spread is None:
        print(_no_spread(proposal), file=sys.stderr)
    if stamp:
        print(f"\nThis number is in your file WITHOUT evidence that adopting "
              f"it catches more. The basis says so; nothing else will.",
              file=sys.stderr)
    return _exit_contract.compose(EXIT_CLEAN, unreadable_floor)


def _no_spread(proposal) -> str:
    """Why an adopted gain went in without a spread, and what that costs."""
    why = ("the engine proposed none with this fit"
           if proposal.gain_sigma is None else
           f"the fit's standard error is {proposal.gain_sigma:.3g}, and a "
           f"spread is a positive number")
    return (f"\nNo gain_sigma was written: {why}. This coupling's projections "
            f"are still graded, against a band that treats the gain as exact "
            f"until a spread is stated with its basis.")


def _render_proposals(candidates, feed_result) -> str:
    """What was fitted, and what each one would be adopted on."""
    if not candidates:
        return ("Nothing fitted. A coupling is fitted only when both ends were "
                "reading across enough paired changes; `detect` reports which.")
    lines = [f"Fitted on a {feed_result.interval_seconds:g}s collection grid:", ""]
    for candidate in candidates:
        lines.append(f"  {candidate.id}")
        low, high = candidate.interval
        lines.append(f"      gain {candidate.gain:.6g}  n {candidate.n}  "
                     f"r_squared {candidate.r_squared:.4g}  "
                     f"interval [{low:.6g}, {high:.6g}]  "
                     f"{candidate.response_model}")
        lines.append(f"      gain_sigma {candidate.spread:.6g}, written beside "
                     f"the gain as its spread"
                     if candidate.spread is not None else
                     "      gain_sigma none -- its projections would be graded "
                     "as though the gain were exact")
        if candidate.declared_gain is not None:
            lines.append(f"      declared {candidate.declared_gain:g} already; "
                         f"a fit that disagrees is a finding, not an edit")
        elif candidate.replay_ran:
            lines.append(f"      replay: detected "
                         f"{candidate.replay.get('detected_before')} -> "
                         f"{candidate.replay.get('detected_after')} of "
                         f"{candidate.replay.get('confirmed')} confirmed "
                         f"(delta {candidate.delta:+d})")
        else:
            lines.append("      replay: none -- "
                         f"{candidate.replay.get('reason')}")
    return "\n".join(lines)


def _cmd_regression(args: argparse.Namespace) -> int:
    """Compare two captures of the same machine across a firmware change.

    Needs no configuration and no BMC: two files and a diff. That matters for where
    it runs -- the flashing station has the captures and often has neither the
    entity-manager tree nor a route back to the machine by the time anyone looks.
    """
    try:
        prefix_map = parse_prefix_map(args.aggregation_prefix or [])
    except ValueError as error:
        # Before either walk is read, so a mistyped flag costs nothing and is not
        # buried under a report. Exit 2: the run could not be made as asked.
        print(error, file=sys.stderr)
        return EXIT_INCOMPLETE

    before = _load_recorded_walk(args.before)
    after = _load_recorded_walk(args.after)

    # The two captures are named, not sorted. `order_walks` exists because a glob
    # hands over lexical order; here the operator has typed which is which, and
    # silently swapping them because their timestamps disagree would report every
    # removal as an addition. So it is checked and SAID, and the run stops: a
    # backwards regression report is worse than no report, because it reads clean.
    if before.captured_at and after.captured_at and before.captured_at > after.captured_at:
        print(f"--before was captured at {before.captured_at} and --after at "
              f"{after.captured_at}, which is the wrong way round. Nothing here "
              f"reorders them: a reversed comparison reports every removal as an "
              f"addition and reads like a clean upgrade.", file=sys.stderr)
        return EXIT_INCOMPLETE

    report = compare_walks(before, after, prefix_map=prefix_map)
    print(regression_as_json(report, before=args.before, after=args.after) if args.json
          else regression_as_text(report, before=args.before, after=args.after))

    if args.strict_fields and not args.json:
        # The AFTER walk's own strictness, so the flag means the same sentence in
        # both commands -- apply field strictness, and require it to be
        # applicable -- rather than sharing a name with `coverage` while doing
        # something else. Two flags spelled alike that mean different things is
        # its own defect, and a worse one than two names.
        #
        # The absolute view and the delta answer different questions. `field_drift`
        # above names what ARRIVED; this names what the firmware carries now,
        # which is what a downstream parser actually meets.
        print(strict_fields_as_text(after, target=args.after))
    strict_floor = _report_uncomparable_fields(before, after, args.strict_fields)

    if not report.complete:
        return EXIT_INCOMPLETE
    stage1 = EXIT_REGRESSION if report.regressions else EXIT_CLEAN
    return _exit_contract.compose(stage1, strict_floor)


def _cmd_validate_attestation(args: argparse.Namespace) -> int:
    """Check an attestation artifact against the format it declares.

    Needs no engine and no hardware: an artifact is JSON, so the person who
    RECEIVES one can run this over a file somebody sent them. That is the point of
    the command existing rather than the rule living inside a CI workflow where only
    the producer can reach it.
    """
    from presence_audit.attestation import validate_attestation

    try:
        artifact = json.loads(Path(args.path).read_text())
    except OSError as error:
        print(f"cannot read {args.path}: {error}", file=sys.stderr)
        return EXIT_INCOMPLETE
    except json.JSONDecodeError as error:
        print(f"{args.path} is not parseable as JSON: {error}", file=sys.stderr)
        return EXIT_INCOMPLETE

    problems = validate_attestation(artifact)
    if problems:
        print(f"{args.path}: {len(problems)} problem(s)", file=sys.stderr)
        for problem in problems:
            print(f"    {problem}", file=sys.stderr)
        return EXIT_REGRESSION

    findings = len(artifact.get("findings") or [])
    declined = len(artifact.get("not_checked") or [])
    print(f"{args.path}: valid {artifact['format']}")
    print(f"  {findings} finding(s), {declined} declined, "
          f"{len(artifact.get('evidence') or [])} with measurements")
    # Printed because a reader's next question is what the judgment rests on, and
    # because an artifact that validates still carries the engine's own limit.
    print(f"  judged under envelope schema_version "
          f"{artifact['engine'].get('schema_version')}")
    print(f"  boundary: {artifact['engine']['boundary']}")
    return EXIT_CLEAN


def _cmd_validate_walk(args: argparse.Namespace) -> int:
    """Check a recorded walk against the format it declares.

    The mirror of `validate-attestation`, and it exists for the same reason: the
    person who RECEIVES the file is the one who needs to check it. A fleet collector
    ingesting captures from machines it does not own has to be able to refuse a
    malformed one in the format's own words.

    Reads the bytes rather than the text, because the digest is over the bytes and
    computing it from a decoded-then-re-encoded string would be a different number
    on any file this build did not write.
    """
    try:
        raw = Path(args.path).read_bytes()
    except OSError as error:
        print(f"cannot read {args.path}: {error}", file=sys.stderr)
        return EXIT_INCOMPLETE
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        print(f"{args.path} is not parseable as JSON: {error}", file=sys.stderr)
        return EXIT_INCOMPLETE

    problems = validate_walk(payload)
    if problems:
        print(f"{args.path}: {len(problems)} problem(s)", file=sys.stderr)
        for problem in problems:
            print(f"    {problem}", file=sys.stderr)
        return EXIT_REGRESSION

    sensors = payload["sensors"]
    errors = payload.get("errors") or []
    print(f"{args.path}: valid {payload['format']}")
    if args.print_digest:
        print(f"  digest          {walk_digest(raw)}")
    print(f"  sensors         {len(sensors)}")
    print(f"  captured at     {payload.get('captured_at') or '(unstamped)'}")
    # Both of these are legal and both change what the file can be used for, so
    # they are stated on every run rather than only when they bite. A capture with
    # no record of object properties supports no strictness question, and one that
    # did not complete cannot be told apart from a machine that lost sensors.
    print(f"  fields observed {'yes' if payload.get('fields_observed') else 'no'}")
    if not sensors:
        print("  ** this walk records no sensors at all. That is a valid capture of "
              "a chassis reporting none, and it is also what a walk of the wrong "
              "target looks like **")
    if errors:
        print(f"  ** INCOMPLETE -- {len(errors)} fetch(es) failed. Absence in this "
              f"walk cannot be told apart from a subtree that was never read **")
        if args.require_complete:
            # The flag is the ask, and asking is what makes the rule apply -- the
            # same shape as `--strict-fields`. Flooring by default would refuse the
            # partial captures `capture` deliberately writes and keeps, which are
            # evidence about which subtree failed and are worth storing.
            print("\ncompleteness was required and this walk did not complete, so "
                  "this run does not exit clean.", file=sys.stderr)
            return EXIT_INCOMPLETE
    return EXIT_CLEAN


_DECLARATION_HELP = (
    "a pdr/1 or fleet-baseline/1 declaration, layered UNDER the manufacturer's "
    "entity-manager files: it covers what they do not declare and never overrides "
    "them. Repeatable. Refused unless it carries a reviewed marker")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bmc-sensor-audit",
        description="Find the sensors that should be reporting and are not.")
    # Consumers run this tool as a SUBPROCESS resolved on PATH, not as an
    # import, so a `>=` in their packaging metadata governs what pip put in
    # their environment and not what actually answers. Until this existed there
    # was no way for them to find out: `--version` exited 2 with an argparse
    # usage error, so a downstream floor could be declared and never checked.
    parser.add_argument("--version", action="version",
                        version=f"bmc-sensor-audit {__version__}")
    # The seam. Global rather than per-subcommand: a vertical supplies the
    # vocabulary the whole run classifies with, not one command's.
    parser.add_argument("--plugin", action="append", metavar="SPEC",
                        help="load a vertical: module[:callable] or path.py[:callable]")
    parser.add_argument("--no-entry-points", action="store_true",
                        help="ignore installed entry points; use only --plugin "
                             "and the environment")

    subparsers = parser.add_subparsers(dest="command", required=True)

    declare = subparsers.add_parser(
        "declare", help="read the configuration and report what it declares")
    declare_source = declare.add_mutually_exclusive_group(required=True)
    declare_source.add_argument("--config", action="append",
                                help="entity-manager JSON file or directory "
                                     "(repeatable)")
    declare_source.add_argument("--from-walk", metavar="WALK",
                                help="derive a pdr/1 CANDIDATE from a recorded walk, "
                                     "for platforms whose sensors arrive as runtime "
                                     "self-description and have no entity-manager "
                                     "entry. Requires --candidate")
    declare.add_argument("--declaration", action="append", metavar="PATH",
                         help=_DECLARATION_HELP)
    declare.add_argument("--candidate", action="store_true",
                         help="required with --from-walk: acknowledges that what is "
                              "written asserts nothing and will be refused until "
                              "somebody reviews it")
    declare.add_argument("--out", help="where to write the candidate")
    declare.add_argument("--platform",
                         help="what this declaration is scoped to, e.g. the board or "
                              "system model. Required with --from-walk")
    declare.add_argument("--firmware",
                         help="the firmware version the walk was taken at; discovered "
                              "inventory moves with firmware")
    declare.set_defaults(func=_cmd_declare)

    coverage = subparsers.add_parser(
        "coverage", help="diff a declaration against what a machine reports")
    coverage.add_argument("--config", required=True, action="append",
                          help="entity-manager JSON file or directory (repeatable)")
    coverage.add_argument("--declaration", action="append", metavar="PATH",
                          help=_DECLARATION_HELP)
    source = coverage.add_mutually_exclusive_group(required=True)
    source.add_argument("--target", help="Redfish base URL, e.g. https://bmc.example")
    source.add_argument("--walk", help="a recorded walk, instead of live hardware")
    _add_connection_flags(coverage)
    coverage.add_argument("--json", action="store_true", help="machine-readable output")
    coverage.add_argument("--include-disabled", action="store_true",
                          help="also expect sensors the config marks Status: disabled")
    coverage.add_argument("--strict-fields", action="store_true",
                          help="also name the properties each sensor object carries "
                               "that the published Redfish schema does not declare")
    coverage.set_defaults(func=_cmd_coverage)

    regression = subparsers.add_parser(
        "regression",
        help="compare two captures of one machine across a firmware change")
    regression.add_argument("--before", required=True,
                            help="a capture taken before the flash")
    regression.add_argument("--after", required=True,
                            help="a capture taken after it")
    regression.add_argument("--json", action="store_true", help="machine-readable output")
    regression.add_argument("--strict-fields", action="store_true",
                            help="also apply field strictness, and exit 2 if either "
                                 "capture carries no record of object properties, "
                                 "so drift cannot be compared")
    regression.add_argument("--aggregation-prefix", action="append", metavar="OLD=NEW",
                            help="declare that sensor names starting OLD in the "
                                 "earlier walk start NEW in this one -- an "
                                 "aggregated satellite republished under a new "
                                 "prefix. Repeatable. Nothing is inferred: a prefix "
                                 "map is a claim about topology")
    regression.set_defaults(func=_cmd_regression)

    detect = subparsers.add_parser(
        "detect", help="coverage plus liveness, in one run and one exit code")
    detect.add_argument("--config", required=True, action="append",
                        help="entity-manager JSON file or directory (repeatable)")
    detect.add_argument("--declaration", action="append", metavar="PATH",
                        help=_DECLARATION_HELP)
    detect_source = detect.add_mutually_exclusive_group(required=True)
    detect_source.add_argument("--target", help="Redfish base URL")
    detect_source.add_argument("--walk", action="append",
                               help="a recorded walk; repeatable, OLDEST FIRST -- "
                                    "stuck-at needs history and one walk is one sample")
    _add_connection_flags(detect)
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
    detect.add_argument("--attest-target-label",
                        help="what the artifact should call the target instead of "
                             "its URL; a BMC hostname names an internal machine and "
                             "an artifact uploaded from CI publishes it")
    detect.add_argument("--ledger", metavar="PATH",
                        help="file the engine's own forecasts into a SQLite "
                             "ledger, so a later run -- or a later --resident "
                             "cycle -- can grade them as they mature. Only what "
                             "the model declares is forecast: a coupling's "
                             "driver, and a coupling once its gain is written "
                             "down. Recorded walks file nothing")
    detect.add_argument("--history", metavar="PATH",
                        help="keep the readings in a SQLite file too. A forecast "
                             "is graded against the reading nearest the moment "
                             "it matures, which one walk never holds, so a "
                             "ledger shared between separate runs needs this. "
                             "Live targets only")
    detect.add_argument("--resident", action="store_true",
                        help="walk the --target again and again in one process, "
                             "grading each cycle's matured forecasts and filing "
                             "the next; needs --ledger")
    detect.add_argument("--every", type=float, metavar="SECONDS",
                        help="how often a --resident run walks; defaults to the "
                             "cadence the supplemental file declares, else 60")
    detect.add_argument("--cycles", type=int, metavar="N",
                        help="stop a --resident run after N walks; without it the "
                             "run continues until interrupted")
    detect.add_argument("--horizon", type=float, metavar="SECONDS",
                        help="how far ahead each forecast is filed; defaults to "
                             "one collection step")
    detect.add_argument("--keep-walks", metavar="DIR",
                        help="write each --resident cycle's walk into DIR, one "
                             "file per cycle named by its instant, so `adopt "
                             "--walk` can fit from the readings this run graded "
                             "and the figures can be re-derived from the raw "
                             "walks. Resident runs only")
    detect.set_defaults(func=_cmd_detect)

    adopt = subparsers.add_parser(
        "adopt",
        help="write a fitted coupling gain into the supplemental file")
    adopt.add_argument("--config", required=True, action="append",
                       help="entity-manager JSON file or directory (repeatable)")
    adopt.add_argument("--declaration", action="append", metavar="PATH",
                       help=_DECLARATION_HELP)
    adopt.add_argument("--walk", required=True, action="append",
                       help="a recorded walk; repeatable, OLDEST FIRST -- a gain "
                            "is fitted from history and one walk is one sample")
    adopt.add_argument("--supplemental", required=True,
                       help="the declarations file to fit from and write into")
    adopt.add_argument("--proposal", metavar="ID",
                       help="which fitted gain to adopt, as `<driver> -> "
                            "<driven>` in your own point names. Omit to list")
    adopt.add_argument("--list", action="store_true",
                       help="show what this run fitted and adopt nothing")
    adopt.add_argument("--surprises", metavar="PATH",
                       help="a surprise corpus for this domain. Without one "
                            "nothing can say whether adopting a proposal would "
                            "catch more, and adopt refuses rather than "
                            "scoring it zero")
    adopt.add_argument("--force", action="store_true",
                       help="adopt even though the replay found no gain, or "
                            "found no corpus. Stamps the basis with which of "
                            "those it was")
    adopt.add_argument("--dry-run", action="store_true",
                       help="print what would be written and write nothing")
    adopt.add_argument("--include-disabled", action="store_true")
    adopt.add_argument("--no-stuck-at", action="store_true",
                       help="do not expect readings to vary")
    adopt.set_defaults(func=_cmd_adopt)

    validate = subparsers.add_parser(
        "validate-attestation",
        help="check an attestation artifact against the format it declares")
    validate.add_argument("path", help="the attestation JSON to check")
    validate.set_defaults(func=_cmd_validate_attestation)

    validate_walk_cmd = subparsers.add_parser(
        "validate-walk",
        help="check a recorded walk against the format it declares")
    validate_walk_cmd.add_argument("path", help="the walk JSON to check")
    validate_walk_cmd.add_argument(
        "--print-digest", action="store_true",
        help="also print the content handle for this file, the same value "
             "capture --print-digest printed when it was written")
    validate_walk_cmd.add_argument(
        "--require-complete", action="store_true",
        help="exit 2 if the walk did not complete; a partial capture is valid "
             "walk/1 and must not be used as a baseline")
    validate_walk_cmd.set_defaults(func=_cmd_validate_walk)

    capture = subparsers.add_parser(
        "capture", help="record a walk to disk, for a before/after gate")
    capture.add_argument("--target", required=True)
    capture.add_argument("--out", required=True, help="file to write")
    capture.add_argument("--etag-cache", metavar="PATH",
                         help="record collection ETags here, and on the next run "
                              "ask the BMC whether the sensor SET changed before "
                              "walking it. Membership only: a threshold edited on "
                              "a sensor that stayed present will not show")
    capture.add_argument("--print-digest", action="store_true",
                         help="also print a SHA-256 content handle for the file, so "
                              "a collector can bind it to a unit on its own side of "
                              "the identity line")
    _add_connection_flags(capture)
    capture.set_defaults(func=_cmd_capture)

    return parser


#: Refusals this tool makes before it can start work. **Every one is exit 2.**
#:
#: A refusal that escapes as a traceback exits `1` -- and `1` means FINDINGS in
#: this family's vocabulary, so a fleet collector reads a misconfigured flag as
#: *the machine has problems*. That is not a cosmetic difference; it is the tool
#: answering a question nobody asked.
#:
#: `CertificatePinError` was added here after exactly that: a pin on an
#: `http://` target refused correctly and crashed, and the consumer saw `1`.
#: A tuple rather than a chain of `except` clauses, so adding a refusal is one
#: edit in one place and the test below can enumerate it.
# PluginError joins these because a vertical that could not be loaded is a
# refusal with something to say, not a traceback from somewhere downstream
# about a vocabulary nobody supplied.
REFUSALS = (CredentialError, CertificatePinError, PluginError)


class _StdoutThatOutlivesItsReader:
    """`sys.stdout`, for a program whose exit code is a claim about a BMC.

    **A reader that stops reading has said something about itself.** Piping a
    report into `head` is an ordinary thing to do, and before this the writer
    died of it two different ways: a report long enough to fill the pipe buffer
    raised out of `print` and Python exited `1`, which this vocabulary reads as
    FINDINGS; a short one survived to the interpreter's shutdown flush, printed
    `Exception ignored` and exited `120`, which is not in the vocabulary at all.

    Both replace a verdict about the hardware with a verdict about the
    terminal. The report is fully rendered before it is printed, so the verdict
    already exists when the pipe closes -- it is kept, the rest of the output is
    dropped, and nothing is said about it.

    Absorbing the error rather than refusing is the whole point: `REFUSALS`
    returns `2`, and a clean audit whose reader left is not an incomplete audit.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self.reader_left = False

    def _abandon(self) -> None:
        """Point the descriptor at nowhere, then stop trying.

        The interpreter flushes `stdout` again on the way out, on bytes this
        stream may still be holding. Without the redirect that second flush
        raises where no `except` can reach it, which is the `Exception ignored`
        line and the `120`.
        """
        self.reader_left = True
        try:
            fileno = self._stream.fileno()
        except (AttributeError, ValueError, OSError):
            return  # captured by a harness rather than piped; nothing to point
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), fileno)
        except OSError:
            pass

    def write(self, text: str) -> int:
        if self.reader_left:
            return len(text)
        try:
            return self._stream.write(text)
        except BrokenPipeError:
            self._abandon()
            return len(text)

    def flush(self) -> None:
        if self.reader_left:
            return
        try:
            self._stream.flush()
        except BrokenPipeError:
            self._abandon()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def main(argv: list[str] | None = None) -> int:
    # Installed before the parser runs, because `--help` prints too and an
    # operator reaching for `--help | head` is the likeliest reader of all.
    stdout = _StdoutThatOutlivesItsReader(sys.stdout)
    sys.stdout = stdout
    try:
        args = build_parser().parse_args(argv)
        # Verticals load BEFORE anything is read. A vocabulary supplied after a
        # declaration has been classified is a vocabulary that did not apply.
        use_entry_points = not getattr(args, "no_entry_points", False)
        _plugins.load_all(getattr(args, "plugin", None) or (),
                          entry_points=use_entry_points)
        if use_entry_points and not _vocabulary.registered():
            # Entry points exist only in an INSTALLED distribution. Run from a
            # source checkout -- `python -m bmc_sensor_audit.cli` -- there are
            # none, and the vertical this package ships would never load. It is
            # bundled, so it registers here, through the same `register()` an
            # outside vertical calls; what it does not get is a private path
            # that skips the door.
            #
            # `--no-entry-points` deliberately does NOT reach this: asking for a
            # run with no vertical must still produce a run with no vertical.
            _bundled.register()
        return args.func(args)
    except REFUSALS as error:
        print(f"{error}", file=sys.stderr)
        return EXIT_INCOMPLETE
    except BrokenPipeError:
        # Not the report: that one is absorbed above and never arrives here.
        # A pipe that broke while writing somewhere else is a failure to
        # deliver, and this tool says so with the code that means it.
        print("the output could not be written: the pipe closed",
              file=sys.stderr)
        return EXIT_INCOMPLETE
    finally:
        # **Flush through the wrapper, before handing the stream back.** A
        # report short enough to sit in the buffer is not written until the
        # interpreter flushes on its way out -- by which point this wrapper is
        # gone and the failure lands where no `except` can reach it. Draining
        # it here is what makes the short-report path survivable at all.
        stdout.flush()
        sys.stdout = stdout._stream


if __name__ == "__main__":
    raise SystemExit(main())
