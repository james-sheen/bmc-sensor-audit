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
from datetime import datetime
from pathlib import Path

from .inventory.declaration_source import (DeclarationSourceError,
                                           candidate_from_walk,
                                           load_declaration_source, merge_sources)
from .inventory.diff import compare
from .inventory.entity_manager import load_declaration
from .inventory.redfish import (CertificatePinError, RedfishClient, Walk,
                                order_walks, validate_walk,
                                etag_cache, membership_unchanged,
                                walk_chassis, walk_digest, walk_from_dict)
from .inventory.regression import compare_walks, parse_prefix_map
from .report import (as_json, as_text, regression_as_json, regression_as_text,
                     strict_fields_as_text)

EXIT_CLEAN, EXIT_REGRESSION, EXIT_INCOMPLETE = 0, 1, 2


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
    from .report import unobserved_reason

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
    from .report import unobserved_reason

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
    return max(stage1, unreadable_floor, strict_floor)


def _cmd_detect(args: argparse.Namespace) -> int:
    """Both stages in one run, and one exit code.

    Stage 1 answers presence; Stage 2 answers liveness for what is present. They are
    composed rather than merged, because they fail differently: a walk that could not
    complete is not a board with missing sensors, and neither is an engine that is not
    installed.
    """
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
    if args.walk:
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
    from .report import detect_as_text, supplemental_as_text

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
        from .report import unattested_notice

        notice = unattested_notice(artifact, args.attest_out)
        if notice:
            print(f"\n{notice}", file=sys.stderr)
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
    return max(stage1, strict_floor)


def _cmd_validate_attestation(args: argparse.Namespace) -> int:
    """Check an attestation artifact against the format it declares.

    Needs no engine and no hardware: an artifact is JSON, so the person who
    RECEIVES one can run this over a file somebody sent them. That is the point of
    the command existing rather than the rule living inside a CI workflow where only
    the producer can reach it.
    """
    from .detect.attestation import validate_attestation

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
    detect.set_defaults(func=_cmd_detect)

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
REFUSALS = (CredentialError, CertificatePinError)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except REFUSALS as error:
        print(f"{error}", file=sys.stderr)
        return EXIT_INCOMPLETE


if __name__ == "__main__":
    raise SystemExit(main())
