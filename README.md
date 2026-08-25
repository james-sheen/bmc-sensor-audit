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

**Released — 0.1.3**, tagged `v0.1.3`, Apache-2.0, on PyPI as
[`bmc-sensor-audit`](https://pypi.org/project/bmc-sensor-audit/). The coverage
diff works end to end and is exercised against the full upstream configuration
corpus; the firmware regression gate and the liveness pass ship alongside it.

**0.1.3 is two contracts and a guard.**

`capture` now prints exactly one `OUTCOME ` line — `walked` or `unchanged` — and
**that line is contract while the rest of its output is prose.** Before it, a
skip and a walk both exited `0` and differed only in a sentence, so a consumer
had to match prose that nothing promised to keep. Reported from outside as issue
#6, against a feature one release old.

The pre-commit hook refuses a commit whose index and working tree disagree,
first, before anything else runs. Every other check in it reads the working tree
while the commit records the index — so when those differ, a green hook is green
about files that are not being committed. This repository shipped a README test
count six short, twice in one day, with its own count check passing both times.

**0.1.2 was what a fleet collector needed and could not reach.** All four of its
surfaces were reported from outside, by building one: `--password-env` and
`--password-file` keep a credential out of argv, where `ps` reads it on a shared
host; `--cafile` and `--pin-sha256` verify a BMC, where `--insecure` had been
the only control; an empty `OLD` in `--aggregation-prefix` declares a prefix that
was **added**, which is the direction aggregation actually goes; and
`capture --etag-cache` asks whether the sensor set changed before walking.

**None of them is a bug.** Each is a surface that was never there, which is a
thing a test suite cannot find — it asks whether what exists is correct. Only a
second program with a real job discovers that what it needed was missing.

`--etag-cache` is deliberately narrower than it was asked to be. A walk is many
resources, so there is no single ETag; and using a `304` per resource means
keeping the previous **body**, which would put raw Redfish payloads on disk. That
is the disclosure the parsed capture exists to avoid. It probes collections
instead, answers *has the sensor set changed*, and prints that it checked
membership and not configuration.

**What 0.1.3 does not have, since silence about it would be the wrong lesson.**
No capture from **physical** hardware. Every fixture here came from an emulator
or from upstream, which is described further down rather than implied away. The
previous version of this paragraph recorded that criterion as honestly open and
*not the same thing as a release blocker* — releasing without it is that sentence
being kept, not quietly dropped. Fleet comparison is not in this repository and
was never going to be: it ships as a separate tool,
[`fleet-sensor-baseline`](https://github.com/james-sheen/fleet-sensor-baseline),
which consumes this one's published surfaces and never imports it.

*Not done yet* and *waiting for X* are different claims, and only the written one
can be checked by a reader — which is the whole argument this tool makes about
sensors, applied to its own status. If a condition gates the next version, it
belongs in this paragraph.

| | |
|---|---|
| Declaration reader | working — 349/349 upstream configs parse at the pinned revision |
| Redfish walk | working — both tree shapes, standard library only |
| Coverage diff | working — three-way classification, thresholds, reverse direction |
| Walk capture | working — `capture` writes a walk for a before/after gate, and `--print-digest` prints a content handle for it |
| Reaching a BMC | `--password-env` / `--password-file` keep a credential out of argv; `--cafile` and `--pin-sha256` verify one, where `--insecure` was the only control |
| Repeat captures | `capture --etag-cache` asks whether the sensor SET changed before walking. Membership only — see below |
| Walk validation | working — `validate-walk` checks a capture against the format it declares, with no engine and no hardware; `walk/1` has a stability statement a downstream pin can pin to |
| Declaration sources | working — `pdr/1` and `fleet-baseline/1` via `--declaration`, layered under the manufacturer's files. A candidate refuses to be consumed until somebody reviews it |
| Firmware regression gate | working — `regression` diffs two captures: removed, renamed, re-thresholded, and pairs across a **declared** aggregation-prefix change |
| Field strictness | working — `coverage --strict-fields`, against property sets derived from DMTF's schemas |
| Mock BMC | working — serves either tree shape over real HTTP, with fault injection |
| Reporting | working — human summary and JSON |
| Hygiene check | working — 8 shipped rules plus a local vocabulary, over files and commit messages, versioned hooks, and a CI sweep neither can be forgotten past |
| Tests | 672 collected with no dependencies installed; the ones that read YAML — the serialised model, and the action definition — skip without PyYAML, so CI installs it; the `[detect]` extra adds an engine canary |
| Liveness detection (Stage 2) | working — `detect` runs coverage and liveness in one pass, one exit code |
| GitHub Action | working — composite, `uses: james-sheen/bmc-sensor-audit@action-v0`; the repository's own CI runs it as a consumer would and pins all three exit codes |
| Fleet comparison | a separate tool — `fleet-sensor-baseline` reads `walk/1` and this one's exit codes, and never imports it |

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

Two captures of one machine, across a firmware change — see
[the regression gate](#the-firmware-regression-gate) below:

```
PYTHONPATH=src python3 -m bmc_sensor_audit.cli regression --before before.json --after after.json
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

A recorded walk is checkable the same way, and by the person who receives one:

```
PYTHONPATH=src python3 -m bmc_sensor_audit.cli validate-walk before.json --print-digest
```

### Repeat captures, and what a short one does not tell you

```
bmc-sensor-audit capture --target https://<bmc> --out walk.json --etag-cache etags.json
```

The first run walks in full and records the ETag of every Redfish *collection*.
The next run asks the BMC whether those collections changed — a handful of
requests instead of a full walk — and skips the walk when they have not.

**It answers membership, and it says so.** A collection's representation is its
member list, so its ETag moves when a sensor appears or disappears. A threshold
edited on a sensor that stayed present changes that sensor's resource and not
its collection, and the skip line tells you that rather than leaving you to
infer it. Drop the flag to compare configuration.

A BMC that does not implement ETags gets a full walk every time and is told so.
*Cannot tell* is never treated as *unchanged*.

There is deliberately no per-resource cache. Using a `304` means having kept the
previous body, and a body cache on disk is exactly the fleet-inventory
disclosure `capture` avoids by writing only the parsed form.

### Reading `capture` from a script

`capture` prints exactly one `OUTCOME ` line, always, and its value is one of
`walked` or `unchanged`:

```
OUTCOME walked
wrote 4 sensor(s) to walk.json
```

```
sensor set unchanged since 2026-08-25T05:15:42Z -- all 2 collection(s) unchanged
  walk.json left as it was; 2 request(s) instead of a full walk
  this answers membership only: ...
OUTCOME unchanged
```

**That line is the contract. Everything else `capture` prints is prose and may
be reworded.**

It exists because a skip and a walk both exit `0`, and they should: a skip is
clean, and a fourth exit code would break the three-valued vocabulary
(`0` clean / `1` findings / `2` could-not-complete) that every tool in this
family shares. So the exit code cannot carry the distinction, and before this
the only signal was a sentence — which a consumer had to match, and which
nothing promised to keep. Reported from outside as issue #6, against a feature
one release old.

### Credentials and TLS

`--password` still works and now says what it costs: it crosses argv, where
`ps` can read it. `--password-env NAME` and `--password-file PATH` do not.

`--cafile PATH` verifies the BMC against a certificate you supply, with hostname
checking left on. `--pin-sha256 FINGERPRINT` requires one exact certificate and
**replaces** chain verification, which is the only thing that works for the
self-signed certificate a BMC ships. Both are alternatives to `--insecure`,
which remains what it was.

`capture --print-digest` prints the same handle when the file is written — the
SHA-256 of the bytes, which `sha256sum` reproduces. A fleet collector binds
`{unit_key, digest, walk_ref}` on its own side of the line; **no identity field
enters `walk/1`, ever.** See [`docs/walk-format.md`](docs/walk-format.md).

Where a platform's sensors arrive as runtime self-description and have no
entity-manager entry — PLDM PDRs and NSM discovery on NVIDIA-managed boards — a
reviewed `pdr/1` supplies the expectation the manufacturer's files do not:

```
PYTHONPATH=src python3 -m bmc_sensor_audit.cli coverage --config <configs> --walk before.json --declaration hgx.json
```

It never overrides entity-manager, every run that used one prints its provenance,
and a candidate is refused until somebody puts their name to it. See
[`docs/declaration-sources.md`](docs/declaration-sources.md).

Or install it from PyPI and use the console script, which needs no `PYTHONPATH`:

```
pip install bmc-sensor-audit
bmc-sensor-audit declare --config <entity-manager-configs>
```

Working from a clone instead, `pip install -e .` puts the same script on the path.

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

## The firmware regression gate

The question a flashing station asks: **which sensors did this firmware update
remove, rename, or re-threshold?** Capture before the flash, capture after, and
compare the two captures directly.

```
bmc-sensor-audit capture --target https://<bmc> --insecure --out before.json
# flash the new firmware, let the BMC come back up
bmc-sensor-audit capture --target https://<bmc> --insecure --out after.json
bmc-sensor-audit regression --before before.json --after after.json
```

Exit `0` clean, `1` something got worse, `2` a walk did not complete. It needs no
configuration and no route back to the machine: two files and a diff, which is
what the station has by the time anyone looks.

**This is not the coverage diff run twice, and the difference is the point.** A
firmware that renames `FAN0_TACH_IL` to `FAN0 TACH IL` still matches the
declaration — the normalised matcher exists to tolerate exactly that spelling
difference — so both coverage runs come back with the same exit code and the same
regressions, while every dashboard, alert rule and trend query keyed on the old
string goes quiet. Measured, in `tests/test_regression_gate.py`.

What fails the gate: a sensor no longer reported, a sensor renamed, a reading lost
while the sensor still reports as enabled, a sensor switched off, a threshold that
moved or vanished, and units that changed under a stable name. What is reported
without failing it: sensors added, thresholds added, a sensor switched back on,
and new properties outside the schema.

**A rename is only claimed where the URI stayed the same, and the URI alone is not
enough.** Some firmware numbers sensors positionally, so inserting one shifts
every URI after it — pairing on URI alone reported two confident renames on a
firmware that had renamed nothing. So the units and resource type have to agree
too. Where a name and a URI both changed, the report shows one removal and one
addition and says so: nothing in two walks settles which addition replaced which
removal, and a wrong guess reads exactly like a right one.

**An aggregation prefix is declared, never inferred.** A BMC that aggregates a
satellite controller republishes its resources under a prefix, and a prefix that
changes across a firmware or topology update moves every name behind it at once —
a mass removal plus a mass addition on a machine that lost nothing.
`--aggregation-prefix OLD=NEW` is the operator stating that the two subtrees are
the same one; the pairings it produces are counted separately and annotated with
the claim they rest on, because nothing here verified it. A name that changed *as
well as* the prefix still refuses to pair. Without the flag nothing auto-pairs —
what the tool does on its own is notice the shape, name both prefixes it saw, and
print the flag that would declare them.

**Field strictness.** `coverage --strict-fields` names the properties a sensor
object carries that the published Redfish schema does not declare — the early
warning that a firmware's output is wandering from what downstream monitoring
parses. The property set is *derived* from DMTF's own schemas by
`tools/derive_redfish_properties.py`, which records the version and SHA-256 of
each document it read; a hand-written list would turn every name its author forgot
into a confident accusation. Annotations are protocol metadata and are not
reported, and neither is anything under `Oem` — that is the extension point the
standard provides, and using it is not drift.

**What it finds never changes an exit code; whether it ran does.** A vendor
extension is permitted by the standard, and a gate that failed on the first one
would be switched off within a week — so a property named here is reported and not
scored. What the gate *does* catch is an undeclared property that **arrived**,
which is a two-version comparison and belongs to `regression`. But a strictness
check that was asked for and **could not run** exits `2`, the same as an
unreadable config or an engine that is not installed: a run that could not
complete the audit it was asked for must not read as clean.

Real firmware carries none: the vendored OpenBMC 2.9.0 capture reports nothing
undeclared across its six sensor objects. That is a small population and it is
stated rather than generalised — what it establishes is that the check does not
fire on ordinary firmware.

**A capture written before this existed says so rather than passing.** `capture`
records the parsed sensor set, so an older capture carries no evidence about any
property; a strictness report over one prints `NOT CHECKED` and the run exits `2`.
Nothing undeclared and nobody looked are different facts, and for a while only the
prose said so while the exit code said clean — reported from outside, and the one
place at that commit where this tool did to itself what it exists to catch
elsewhere.

**`regression --strict-fields` says the same sentence about the comparison.**
Between two walks, drift is computed only when *both* captures recorded object
properties, and by default a run that cannot compare them says so and is judged
on the removals, renames and thresholds it could compare — because flooring by
default would fail every gate run against an older baseline, over a question
nobody asked. The flag asks it. With it, an uncomparable run exits `2` and names
which capture is the stale one, and the after-walk's own strictness is printed
alongside the delta: `field_drift` names what **arrived**, the section names what
the firmware carries **now**.

## The GitHub Action

The gate costs five lines in someone else's firmware pipeline:

```yaml
- uses: james-sheen/bmc-sensor-audit@action-v0
  with:
    config: configs/
    walk: captures/after-flash.json
```

It is a composite action — `actions/setup-python` plus a pinned `pip install` is
the whole machine. No Docker image, no JavaScript runtime, nothing to audit that
the people auditing the tool are not already auditing.

**Inputs**

| Input | Required | Default | Notes |
|---|---|---|---|
| `config` | yes | — | file or directory, walked recursively; newline-separated for several |
| `walk` | one of `walk`/`target` | — | newline-separated for several, **oldest first** — liveness reads them as a series |
| `target` | one of `walk`/`target` | — | live Redfish base URL; mutually exclusive with `walk` |
| `username` / `password` | with `target` | — | pass from `secrets`; never written into the generated script |
| `insecure` | no | `false` | lab TLS; BMCs ship self-signed certificates |
| `mode` | no | `detect` | `coverage` for Stage-1-only pipelines, which installs no engine |
| `attest` | no | `false` | writes `attestation.json` and uploads it; requires `mode: detect` |
| `attest-target-label` | no | — | site-neutral name to record instead of the URL — see the warning below |
| `python-version` | no | `3.12` | the tool requires 3.10 or newer |

**Outputs**

| Output | Meaning |
|---|---|
| `exit-code` | the tool's own `0` / `1` / `2`, verbatim |
| `verdict` | `clean` / `regressions` / `incomplete` — the same fact as a word |

**The step fails on any nonzero exit. That is the gate.** A workflow that needs to
tell the three apart sets `continue-on-error: true` and reads the outputs, and the
contract it reads is the tool's own: `2` is *could not complete*, and
**could-not-complete never reads as clean**. The outputs are written before the
step exits, so a failing run is still readable — a step that dies without writing
them leaves a consumer branching on an empty string, and the runs worth branching
on are the ones that failed.

A misconfigured run — `walk` and `target` together, neither of them, or `attest`
without `detect` — reports `2` as well. It has not judged the machine it was
pointed at, and that is the same fact.

**Two things are versioned here, so the tags say which.** Bare `vX.Y.Z` is the
tool on PyPI; `action-vX.Y.Z` is this action. They are separate artifacts with
separate interfaces, and one tag namespace would otherwise have to serve both —
which is how a repository ends up unable to release its own 1.0.

| Tag | Versions | Installs |
|---|---|---|
| `v0.1.3` | the tool, on PyPI | — |
| `action-v0` | this action, moving — tracks the latest `action-v0.x.y` | `bmc-sensor-audit>=0.1,<0.2`, with the `[detect]` extra when `mode: detect` |

**`action-v0`, not `action-v1`, on purpose.** A `1.0.0` is a promise that the
input surface is stable and that breaking it costs a major bump. Nobody outside
this repository has invoked this action yet, so that promise would be the one
claim here not backed by a measurement — which is not a thing this project gets
to do, given what the rest of it argues. It moves to `action-v1` when there is
someone to make the promise to.

The pin lives in `action.yml` and moves only in a release change, so the tag you
wrote keeps giving you the behaviour you tested against. Note that the pin widens
when a new tool release proves compatible; that is not a breaking change and does
not move the action's major, because nothing you wrote has to change.

If you would rather nothing move under you at all, pin the full commit SHA —
`uses: james-sheen/bmc-sensor-audit@<sha>`. Both are real positions and the
tension is not worth pretending away: the moving tag trusts this repository, the
SHA trusts nothing and updates nothing.

**On `attest` and live targets.** An attestation records what was checked and what
was declined, and uploading it publishes whatever `--target` was to anyone who can
read the artifact. That is a different door out of your pipeline than the log is,
and no secret scanner reads it. When `attest` is on against a `target` with no
`attest-target-label`, the action emits a warning rather than refusing — you may
have meant it, and a refusal there would be the wrong size of response.

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

Two example files ship in [`examples/supplemental/`](examples/supplemental). One is
**worked** — every entry in `ampere-mtjade.json` is established by the vendored
configuration plus the PMBus commands its labels name, and it declares no
redundant group and no counter, because neither can be established from a
configuration file. The other is a **template**, and its placeholder names match
nothing on purpose: a run against it unedited stops and names every line still to
be filled in, rather than checking nothing and reporting agreement.

**A number the file leaves out is one the engine chooses.** A redundant group with
no `tolerance` and a flow with no `loss_margin` are still judged — against the
engine's defaults, which from the report were indistinguishable from numbers an
operator picked. `detect` now names them. That is the same argument the required
`basis` makes, one level down: a check running against an unspecified threshold is
a working check, not a specified one.

**Burn-in.** How many walks a liveness run needs, why the station's interval is
not in that arithmetic, and the window that silently caps it:
[`docs/burn-in.md`](docs/burn-in.md).

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

- **`--strict-fields` has not met an NVIDIA-class capture.** The property sets are
  derived from DMTF's schemas and are resource-type aware, and every capture they
  have been run against is an OpenBMC one. What an energy or power `Sensor` object
  on a DGX or HGX board reports, and whether a paginated Sensors collection walks
  correctly, is **unmeasured** — so this is a verification item and it ships
  nothing. When it is measured, the sets grow **from the spec and never from the
  observation**: the rule that built them is the rule that grows them, and a set
  extended to match one machine is a set that agrees with that machine by
  construction.
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
