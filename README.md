# bmc-sensor-audit

Find the sensors that should be reporting and are not.

Every monitoring stack can see the sensors that report. None of them can see the
ones that went absent — removed by a firmware upgrade, disabled at the factory,
or reading a value that quietly stopped moving. Those cases are documented by
every major server vendor, and in each one the gap was found by a person
noticing rather than by monitoring.

Prometheus has `absent()`. `ipmi_exporter` publishes `ipmi_sensor_state`. Redfish
carries `Status: {Health, State}` per sensor. The signal exists — it fails on the
*expectation source*. `absent()` needs a name to look for, and when a firmware
upgrade removes three sensors you had never heard of, no alert rule exists for
them and none ever will.

OpenBMC is unusual in that the authoritative declaration of what *should* exist
is a machine-readable file under version control: `entity-manager` JSON, every
sensor with all four thresholds. No monitoring stack reads it. This tool is the
diff between what that file declares and what the machine actually reports.

## Status

**Stage 1, in progress.** The coverage diff works end to end and is exercised
against the full upstream configuration corpus. Not yet released, not yet
installable from an index, and there is no tagged version.

| | |
|---|---|
| Declaration reader | working — 247/247 upstream configs parse |
| Redfish walk | working — both tree shapes, standard library only |
| Coverage diff | working — three-way classification, thresholds, reverse direction |
| Walk capture | working — `capture` writes a walk for a before/after gate |
| Mock BMC | working — serves either tree shape over real HTTP, with fault injection |
| Reporting | working — human summary and JSON |
| Hygiene check | working — 10 rules, versioned pre-commit hook, plus a CI sweep the hook cannot be forgotten past |
| Tests | 136 collected, all passing or skipped |
| Liveness detection (Stage 2) | not started |
| Fleet comparison (Stage 3) | not started |

**Acceptance criteria, honestly**: 1, 3 and 4 are met, and **criterion 1 is now
reproducible by a reader** rather than only by us — nine upstream configurations
are vendored, and each documented finding has a test that runs against them.
**Criterion 2 is met only
against synthetic fixtures** — both tree shapes are proven, but by fixtures this
project's own mock generated, so the same code wrote and read them. That proves
the walker handles each shape and that the recorded format round-trips; it
cannot prove either shape resembles a real BMC. A capture from real hardware is
still wanted, and until there is one, criterion 2 is not honestly closed.

## Try it

No dependencies and no installation. The package lives under `src/`, so point
Python at it:

```
PYTHONPATH=src python3 -m bmc_sensor_audit.cli declare --config <entity-manager-configs>
PYTHONPATH=src python3 -m bmc_sensor_audit.cli coverage --config <configs> --target https://<bmc> --insecure
PYTHONPATH=src python3 -m bmc_sensor_audit.cli capture  --target https://<bmc> --out before.json
PYTHONPATH=src python3 -m bmc_sensor_audit.cli coverage --config <configs> --walk before.json --json
```

Or install it and use the console script, which needs no `PYTHONPATH`:

```
pip install -e .
bmc-sensor-audit declare --config <entity-manager-configs>
```

`--config` takes a file or a directory, repeatably; a directory is walked
recursively, because a platform's declaration is normally several files and
asking someone to enumerate them invites them to miss one.

**Exit codes are the CI interface**: `0` clean, `1` regressions found, `2` the
run could not be completed. The third is distinct on purpose — a pipeline that
reads "could not reach the BMC" as "sensors are missing" will fail a good
firmware image, and it only has to do that once before nobody trusts the gate.

## What it finds

Presence is three-valued, not two. Present and reading, present but disabled or
unreadable, and entirely absent are three different hardware conditions with
three different responses, and the middle one is the case this tool exists for —
a disabled sensor does not appear in most BMC web UIs at all.

Beyond presence: thresholds that moved between the config and the machine,
sensors the machine reports that nothing declares, and defects in the
declaration itself. That last category is worth its own sentence — **a
contradiction in the expectation source is invisible to anything that only
watches readings**, and running the reader across the upstream corpus found two.

## What reading the real configs changed

The parser is shaped by measuring 247 upstream configuration files rather than
by reading the format documentation. Five things the documented example does not
prepare you for, each of which silently corrupts a naive implementation:

- **Ten of the files are not strict JSON.** They carry C-style block comments.
  A tool that skips unparseable files reports their sensors as *undeclared*
  rather than *unread* — a false clean bill of health for the whole board.
- **The top level is an object in 178 files and an array in 59.**
- **One `Exposes` entry can declare several sensors.** Hot-swap controllers carry
  a `Label` per rail; 748 entries use them and one has 33. Counting entries
  counts boards, not sensors.
- **Roughly one name in eight is a runtime template** (`$bus`, `$address`,
  `$index`). Compared literally, about 470 sensors read as missing on every
  healthy board. `CONFIG_FORMAT.md` documents three such variables; the corpus
  uses five.
- **The threshold-name vocabulary has fifteen spellings, not four.** `Direction`
  has exactly two values across all 10,687 thresholds, so it — not the name — is
  what decides which side a threshold guards. A name whose severity level is
  unrecognised is reported rather than discarded: a closed enum with a missing
  member misclassifies confidently instead of failing.

## Two defects found in the upstream corpus

Both are hot-swap controller temperature thresholds named `upper critical` and
given `Direction: less than`, in `meta/fbyv2.json` and `meta/fbyv35.json`. The
named condition cannot alarm, and the healthy range does. This is the same
polarity inversion the tool is built to catch downstream, sitting in the
declaration that every downstream generator trusts.

## Licence

Apache-2.0. See `LICENSE` and `NOTICE`.

The same terms as `arbiter-engine`, which this depends on from Stage 2, and as
OpenBMC's own `entity-manager`, which it reads. A consumer who already has both
in their tree acquires no new licence obligation by adding this.

## Hygiene

This repository is **authored in public**. There is no private tree, no scrub and
no staging step between what gets written here and what the world reads. That
removes an entire class of bug — nothing can be mangled in translation — and it
removes the safety net at the same time.

```
git config core.hooksPath .githooks      # once, per clone
python3 tools/hygiene_check.py --all     # sweep the whole tree
```

**The hook cannot install itself, and that first line is the whole problem.** Git
refuses to let a cloned repository set its own `core.hooksPath`, which is the
right refusal — a repo that could would be arbitrary code execution on clone. So
activation is manual, per clone, and a manual step is one that gets skipped. It
was skipped here: `core.hooksPath` was unset in the authoring clone for the first
three commits, so the only check this project had never ran on any of them. The
failure mode of a hook that is not installed is silence — commits simply succeed,
exactly as they do when the check passes.

Hence two more layers, because one opt-in step is not a gate:

- **CI runs the same sweep on every push and pull request**, on the server, where
  no local setting can switch it off. It is not schedule-only, deliberately.
- **The test suite fails if the hook is not enabled in your clone**, so you find
  out in one line rather than not at all.

Ten rules, each with a test that plants its hazard and a test that keeps it quiet
on something similar and harmless. The second half is what keeps the check
usable: a rule that goes red for a legitimate reason on every run teaches
everybody to skip the whole thing.

The class worth naming, because no general checklist would have it: **a Redfish
walk of a real machine returns serial numbers, part numbers, asset tags and MAC
addresses**, and the natural way to build a realistic fixture is to capture one
and commit it. `capture` writes only the parsed sensor set for that reason; the
hook catches the paths that go around it.

A line ending in a comment reading `hygiene: synthetic` is skipped. It exists
because the redaction tests must contain realistic-looking asset tags in order to
assert those tags never reach a capture — the check and the test want the same
strings for opposite reasons. The cost is stated rather than hidden: a genuine
secret pasted onto a marked line is invisible to the check. The marker is per
line and never per file, so it stays visible at the site and in review.

**What it cannot do.** It matches patterns, so it finds the shapes it knows and
nothing else. Treat a clean run as the absence of known shapes, not as evidence
a diff is safe to publish.

## Still open

- **The corpus-wide totals are still not reproducible, though the findings now
  are.** Nine upstream configurations are vendored verbatim under
  `tests/fixtures/upstream/`, with Intel's copyright and the upstream licence
  carried alongside, and every documented parser finding is now runnable from a
  clone. What they cannot reproduce are the counts — 247 files, 5,496 sensors,
  661 templated names, 10,687 thresholds — which come from the full corpus and
  remain measurements against a checkout only we have. **No upstream revision is
  pinned**, because there was no version-control metadata to read.
  `tests/fixtures/upstream/README.md` records what each file is for, what the set
  does not cover, and one open lead it found.
- **The recorded walk fixtures are synthetic.** See the Status note. Both tree
  shapes are proven against fixtures this project's own mock generated, which
  proves the walker and not the shapes.
