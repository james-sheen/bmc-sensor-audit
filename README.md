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

**On the missing tag, since silence about it would be the wrong lesson.** The
work a release needs is done and under test: the community and citation files,
the attestation format with a shipped validator, and the walk-ordering fix that
made a run's idea of *now* verifiable. The one acceptance criterion still
honestly open is a capture from **physical** hardware — described further down,
and not the same thing as a release blocker. No other condition is recorded here.

That last sentence is the point. *Not done yet* and *waiting for X* are different
claims, and only the written one can be checked by a reader — which is the whole
argument this tool makes about sensors, applied to its own status. If a condition
does gate the tag, it belongs in this paragraph.

| | |
|---|---|
| Declaration reader | working — 349/349 upstream configs parse at the pinned revision |
| Redfish walk | working — both tree shapes, standard library only |
| Coverage diff | working — three-way classification, thresholds, reverse direction |
| Walk capture | working — `capture` writes a walk for a before/after gate |
| Mock BMC | working — serves either tree shape over real HTTP, with fault injection |
| Reporting | working — human summary and JSON |
| Hygiene check | working — 8 shipped rules plus a local vocabulary, over files and commit messages, versioned hooks, and a CI sweep neither can be forgotten past |
| Tests | 369 collected with no dependencies installed; two of them scan the serialised model and skip without PyYAML, so CI installs it; the `[detect]` extra adds an engine canary |
| Liveness detection (Stage 2) | working — `detect` runs coverage and liveness in one pass, one exit code |
| Fleet comparison (Stage 3) | not started |

**Acceptance criteria, honestly**: 1, 3 and 4 are met, and **criterion 1 is now
reproducible by a reader** rather than only by us — thirteen upstream configurations
are vendored, and each documented finding has a test that runs against them.
**Both tree shapes are now exercised against real firmware, at different ages.**
The modern `Sensors` shape is proven against a capture from upstream `bmcweb`
under QEMU — `tests/fixtures/walk_qemu_bletchley.json`, 28 sensors, provenance in
the file. The **deprecated `Thermal` shape** is proven against a second capture —
`tests/fixtures/redfish_witherspoon_2_9_0.json`, the **OpenBMC 2.9.0 witherspoon
release image (published 2021)** booted under the same emulator: 2 temperatures
and 4 fans, real values with real legacy thresholds, parsed with no errors, while
that firmware's modern `Sensors` collection is **empty**. Nothing in either walk
was written by this project. That fixture holds the **verbatim Redfish documents**
rather than our parsed output, and the tests replay them over the same real
`http.server` and client the mock uses — so the walker itself is what is under
test, not its own output.

Two honest floors on that second one. **`Power` is still unproven**: the 2.9.0
machines serve a `Power` document whose only populated array is `PowerControl`,
which carries a power-limit object and no reading — so the deprecated *voltage*
path has still never seen real data. And the reason current images cannot supply
either is a **build option, not a removal**: `bmcweb` still carries the handlers,
behind `redfish-allow-deprecated-power-thermal`, whose meson default is
`disabled`. Measured 2026-08-18 by grepping all 19,340 tracked files of
`openbmc/openbmc` at master — 32 vendor layers, no submodules — the flag appears
exactly once, as its own definition, absent from bmcweb's default `PACKAGECONFIG`
with **no machine appending it**. Its description says it will be removed June
2026, a date already past. So the deprecated evidence here has a shelf life
measured in upstream releases, and old published images are what supply it: the
2.9.0 release carries six, of which four have QEMU machine models. A capture from
**physical** hardware is still wanted — for sensor-population realism, real fault
states, and vendor Redfish dialects other than `bmcweb` — and until there is one,
criterion 2 is not honestly closed.

## Try it

No dependencies and no installation. The package lives under `src/`, so point
Python at it:

```
PYTHONPATH=src python3 -m bmc_sensor_audit.cli declare --config <entity-manager-configs>
PYTHONPATH=src python3 -m bmc_sensor_audit.cli coverage --config <configs> --target https://<bmc> --insecure
PYTHONPATH=src python3 -m bmc_sensor_audit.cli capture  --target https://<bmc> --out before.json
PYTHONPATH=src python3 -m bmc_sensor_audit.cli coverage --config <configs> --walk before.json --json
```

Coverage plus liveness in one run, once the optional extra is installed:

```
PYTHONPATH=src python3 -m bmc_sensor_audit.cli detect --config <configs> --walk before.json --walk after.json
```

`detect --attest-out FILE` writes a per-run record of what was checked, what was
declined, and the measurement behind each finding. Anyone can check one — no
engine and no hardware, because an artifact is just JSON:

```
PYTHONPATH=src python3 -m bmc_sensor_audit.cli validate-attestation attestation.json
```

The format is documented in
[`docs/attestation-format.md`](docs/attestation-format.md).

Or install it and use the console script, which needs no `PYTHONPATH`:

```
pip install -e .
bmc-sensor-audit declare --config <entity-manager-configs>
```

**Going to run the test suite? Enable the hook first.**

```
git config core.hooksPath .githooks
```

The suite **fails without it, deliberately** — the pre-commit hygiene check cannot
install itself, so an unenabled hook is silent, and silence is the one thing a
safety check must never be. Full reasoning under [Hygiene](#hygiene). This line is
here rather than only there because a reader meets the red before they reach the
explanation.

`--config` takes a file or a directory, repeatably; a directory is walked
recursively, because a platform's declaration is normally several files and
asking someone to enumerate them invites them to miss one.

**Exit codes are the CI interface**: `0` clean, `1` regressions found, `2` the
run could not be completed. The third is distinct on purpose — a pipeline that
reads "could not reach the BMC" as "sensors are missing" will fail a good
firmware image, and it only has to do that once before nobody trusts the gate.

"Could not be completed" includes a configuration file that could not be read,
and it includes the case where the rest of the directory read fine. The sensors
that file declared are unverifiable, not absent, so the run has not checked the
board it was pointed at — one corrupt file in an otherwise good directory is the
version of this that looks clean from every other angle.

## What it finds

Presence is three-valued, not two. Present and reading, present but disabled or
unreadable, and entirely absent are three different hardware conditions with
three different responses, and the middle one is the case this tool exists for —
a disabled sensor does not appear in most BMC web UIs at all.

**Not every `Exposes` entry is a sensor**, and treating them alike is how a tool
reports a healthy board as broken. PID control loops, stepwise fan curves, EEPROMs,
firmware images, I2C muxes and GPIO presence detectors are declared exactly like
sensors and can never appear in a Redfish `Sensors` collection — **2,069 of 8,809
upstream declarations, about 23 %**, measured at the pinned revision. They are
classified out of the expectation and counted, never silently dropped.

The classification is **three-valued for the same reason presence is**: a `Type` this
build has never seen is reported as *unrecognised* rather than forced into whichever
bucket the default happens to be. An unrecognised type never produces an absence
finding — claiming a regression for something the tool cannot classify is the false
positive this exists to remove — and it is printed by name so the classification can
be corrected rather than quietly trusted.

Beyond presence: thresholds that moved between the config and the machine,
sensors the machine reports that nothing declares, and defects in the
declaration itself. That last category is worth its own sentence — **a
contradiction in the expectation source is invisible to anything that only
watches readings**, and running the reader across the upstream corpus found two.

## What reading the real configs changed

The parser is shaped by measuring the upstream configuration corpus rather than
by reading the format documentation. Every number below was derived at
`openbmc/entity-manager@0ada0483` — **349 files** — and is stated with that basis
because a count without one cannot be re-derived, only believed.

Five things the documented example does not prepare you for, each of which
silently corrupts a naive implementation:

- **Ten of the files are not strict JSON.** They carry C-style block comments.
  A tool that skips unparseable files reports their sensors as *undeclared*
  rather than *unread* — a false clean bill of health for the whole board.
- **The top level is an object in 264 files and an array in 75**, with the
  remaining ten being the JSONC files above, whose top level a strict parser
  never sees at all.
- **One `Exposes` entry can declare several sensors.** Hot-swap controllers carry
  a `Label` per rail; 1,132 entries use them and one declares 33. Counting
  entries counts boards, not sensors. **The rail set comes from the entry's
  `Labels` array, which declares it** — not from its thresholds, which are only a
  proxy for it. Across the vendored files `Labels` declares 149 rails and 34 carry
  a threshold; reading the proxy left the other 115 unconstructed, so nothing
  expected them and their absence could never be reported. A rail naming itself
  through `<label>_Name` takes that name, because that is the name the machine
  publishes.
- **709 of 8,809 declared sensor names are runtime templates** — about one in
  twelve (`$bus`, `$address`, `$index`). Compared literally, every one of them
  reads as missing on a healthy board. `CONFIG_FORMAT.md` documents three such
  variables; the corpus uses five.
- **The threshold-name vocabulary has fifteen spellings, not four.** `Direction`
  has exactly two values across all 15,860 thresholds, so it — not the name — is
  what decides which side a threshold guards. A name whose severity level is
  unrecognised is reported rather than discarded: a closed enum with a missing
  member misclassifies confidently instead of failing.

## Liveness (Stage 2)

A sensor can be present, enabled, and reporting a perfectly plausible number that
stopped being a measurement some time ago. No threshold check can see that, because the
value is in range — it is in range because it is frozen. `detect` runs the coverage diff
and then asks [`arbiter-engine`](https://pypi.org/project/arbiter-engine/) the liveness
question about whatever is still reporting.

Install it with the extra, since Stage 1 stays dependency-free:

```
pip install 'bmc-sensor-audit[detect]'
```

**What it finds.** Readings past an upper bound; readings beneath a lower bound; a
series that has stopped moving while the model says it should vary; two readings an
operator declared redundant that no longer agree; an input/output power imbalance
past a declared loss margin; a counter that has gone backwards; and a sensor Stage 1
said was reading whose value never reached the model, which means the name mapping is
wrong.

**Lower bounds are declared, not negated.** They used to be: the engine's bounds check
was upper-only, so a lower bound became a second indicator carrying the *negated*
reading against negated thresholds, and the report un-inverted the wording on the way
out. Engine 0.1.7 takes `lower_warning` and `lower_critical` directly, so that whole
mechanism is deleted. A sensor declaring both pairs gets a band.

**Three things the configuration cannot say.** Whether two sensors measure the same
quantity, whether a reading is cumulative, and what conversion loss a power stage is
allowed — none of these are in `entity-manager`, and none can be derived from what
is. A TMP421's two channels are its own die and an external diode, which differ by
tens of degrees on a working board; six SLED sensors on six different parts carry
identical thresholds. So they come from an operator-declared file passed with
`--supplemental`, each entry carrying a required `basis`. The generator lists
multi-channel parts as *candidates* and asserts nothing about them.

**A per-run record.** `--attest-out` writes what was checked, what was **declined**,
and the measurement behind every finding — the reading, the threshold it crossed and
which side of the band it was. It carries the engine's own boundary statement
verbatim rather than paraphrased.

**What it deliberately does not find.** Oscillation at periods other than two samples. A
fan hunting on a four-sample cycle produces no finding and no decline. That is a gap in
what is detected and **not** a defect in the engine: its metric is defined for
alternating values, and a period-four square wave cannot match it by construction. The
measurement is in [`docs/stage2/s2-oscillation-not-a-defect.md`](docs/stage2/s2-oscillation-not-a-defect.md).

**How to read the declines.** They are the point, not noise:

| Decline | Means | Exit code |
|---|---|---|
| `insufficient_samples` | liveness is warming up — one walk is one sample and stuck-at needs about ten | `0`, reported |
| `missing_property` | Stage 1 said this was reading and its value did not arrive — a mapping bug | `1` |
| anything else | a reason this build does not recognise | `0`, reported prominently |

The third row is deliberate. The engine does not publish its decline vocabulary, so an
unfamiliar reason is a certainty over time rather than a hypothetical, and filing it
under the nearest familiar one would reclassify the case and report it confidently.
`--strict-declines` escalates the first and third rows to `1`.

**Stuck-at detection is exercised against real firmware readings, under ground
truth somebody controlled** — `tests/fixtures/stuck_at_qemu_bletchley.json`, 28
consecutive walks in which one sensor was driven to a new value before each of the
first twelve and then left alone. It is silent while driven and flagged once
frozen, and over the driven phase the sensors the engine flags are exactly the
sensors that did not move, derivable from the recording without asking the engine.

**What is still open is narrower than it was.** Freezing a register through an
emulator monitor is an induced fault, not a sensor failing on its own, so **no
real BMC has yet been watched going quiet by itself**, and QEMU wires a subset of
any board's devices by construction. Both need physical hardware. They no longer
need it in order to show the pathway works.

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

There is a second hook. **A commit message is the one published surface that can
never be corrected** — a file can be fixed in the next commit, a message cannot —
and the file-scanning check could not see it. `commit-msg` runs the same rules
over the message, so a token or an internal identifier cannot reach permanent
history through the only door with no lock on it. It also refuses a few objective
things: an over-long subject, a trailing full stop, a missing blank line, and this
project's *internal* commit format, which carries identifiers that must not be
published.

It deliberately does **not** enforce imperative mood. The convention is real —
`git revert` composes a sentence around your subject, so a declarative one inverts
— but it is not checkable: the clearest violation here began with the word
`declare`, which any first-word heuristic reads as a perfect imperative. A rule
that refuses honest messages gets `--no-verify`d, and that switches off the leak
rules too. So it is documented and left to judgement. For the same reason a
suspicious bare number is a **note**, not a refusal: measured against real history
the rule flagged five numbers and two were legitimate.

Security reports go through [SECURITY.md](SECURITY.md) — privately, because for
this project the report is often the disclosure.

Eight shipped rules, each with a test that plants its hazard and a test that
keeps it quiet on something similar and harmless.

**Site-specific rules do not ship.** A rule that forbids a private name has to
spell that name out, so keeping such rules in tracked source would publish exactly
what they exist to protect — in the file whose job is preventing that. They live
in an untracked `.hygiene-local.json` instead, loaded if present:

```
{"rules": [{"name": "internal_ticket",
            "pattern": "(?<![\\w-])XY-\\d{2,}(?![\\w-])",
            "why": "an internal ticket identifier"}]}
```

Every run says which vocabulary is active, because a check that quietly stops
looking for half of what it knows is worse than one that never knew. An unreadable
vocabulary file is a hard error rather than a reduced run, for the same reason. The second half is what keeps the check
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
  are.** Thirteen upstream configurations are vendored verbatim under
  `tests/fixtures/upstream/`, with Intel's copyright and the upstream licence
  carried alongside, and every documented parser finding is now runnable from a
  clone, **pinned to `0ada048303bb007c9d7ec3a6a90433169f05dd99`**. What they
  cannot reproduce are the corpus-wide counts — 349 files, 8,809 sensors, 709
  templated names, 15,860 thresholds — which need the full corpus. The first
  attempt at this directory was unpinned, and within a day two of its nine files
  had been renamed upstream and every count had moved; the shapes did not.
  `tests/fixtures/upstream/README.md` records what each file is for, what the set
  does not cover, and one open lead it found.
- **Two recordings are real; the rest are synthetic.**
  `tests/fixtures/walk_qemu_bletchley.json` was captured from upstream `bmcweb`
  under QEMU and proves the modern `Sensors` shape against firmware this project
  did not write. `tests/fixtures/stuck_at_qemu_bletchley.json` is 28 consecutive
  walks of that same machine in which one sensor was driven to a new value before
  each of the first twelve and then left alone — so **liveness detection is
  checked against ground truth somebody controlled**, on readings real firmware
  served, rather than against a series this project generated. Which sensors sat
  still is derivable from the recording itself, and over the driven phase the
  engine's verdict equals that derived set exactly. Freezing a register through an
  emulator monitor is an experiment, not a sensor failure, and the fixture says so.
  The two `walk_*_tree.json` fixtures remain generated by this
  project's own mock, and the deprecated `Thermal`/`Power` shape is still proven
  only by them. The capture also has a floor of its own: QEMU wires a subset of
  the board's devices, so the population is partial by construction, and a
  synthetic FRU had to be written into the emulated EEPROM before entity-manager
  would instantiate the board at all.
