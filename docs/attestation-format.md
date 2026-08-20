# `bmc-sensor-audit/attestation/1`

A per-run record of what was checked, what was **declined**, and the measurement
behind every finding. Written by `detect --attest-out FILE`, checked by
`validate-attestation FILE`, and produced daily by this repository's canary from
vendored inputs so there is always a current example to read.

**The versioning rule, first, because everything below depends on it.** Within
`/1` this format may gain keys and may never change the meaning of one. A meaning
change is `/2`. So a reader may write code that ignores keys it does not know, and
may not write code that assumes a key it does know has kept a meaning across a
major bump. This mirrors the discipline the engine applies to its own envelope,
and for the same reason: the consumer is downstream of two contracts, not one.

## Reading one without installing anything

```
bmc-sensor-audit validate-attestation attestation.json
```

**No engine and no hardware required.** An artifact is JSON, and auditing one is a
Stage 1 operation — a recipient should not have to install a detection engine to
check a file somebody sent them. Exit `0` valid, `1` problems listed on stderr,
`2` the file could not be read.

## Fields

Every key the builder writes today. *Stability* is the promise made within `/1`.

| Key | Type | Meaning | Stability |
|---|---|---|---|
| `format` | string | `bmc-sensor-audit/attestation/1`. The contract this file claims. | Fixed; a change is a new major |
| `target` | string | What was audited — a Redfish URL, or the path of the recorded walk. **May name an internal host; see the note below.** | Present, may be null for an unnamed source |
| `engine.schema_version` | integer | The envelope contract the judgment was made **under**. This is the artifact's provenance chain into the engine, and it is not the engine's release number. | Present whenever the engine stamped one |
| `engine.boundary` | string | **The engine's own statement of what its evidence does and does not establish**, quoted verbatim. | Present **whenever `evidence` is**; wording is the engine's, not ours |
| `checked` | object | `{invariants, entities}` — the denominator. How many questions were asked, over how many things. | Keys may be added |
| `findings` | array | What went wrong. Each carries `sensor` (the name on the board), `entity_type` (the sanitised form the engine used), `axiom`, `severity`, `problem_type`, and `statement` — the finding rendered in the operator's own vocabulary. | Entries may gain keys |
| `not_checked` | array | **What could not be evaluated, and why.** Each carries `sensor`, `axiom`, `reason` and `detail`. | Entries may gain keys |
| `evidence` | array | The numbers. One per finding: `sensor`, `axiom`, `problem_type`, `confidence`, `boundary`, and `measurement` — the reading, the threshold it crossed, which side of the band, as the engine reported them. | One per finding; see the invariant below |
| `unattested` | array | Problem types the engine declined to attest, each with its reason. Empty is the normal case. | Present, possibly empty |
| `unread_feeds` | array | Observations that were fed and that nothing in the model read. | Present, possibly empty |

## `target` leaves through a different door from everything else

**An attestation is the first thing this project produces that is published
without being committed.** The hygiene rules guard files on their way into the
repository, and their founding case is exactly this class of problem: a Redfish
walk of a real machine returns serials and asset tags, so `capture` writes only
the parsed sensor set. That perimeter is drawn around commits.

An artifact uploaded from CI is not a commit. `target: https://bmc-rack12.corp.internal`
publishes an internal hostname into a channel no hook scans, and the field is
populated by default with whatever `--target` was.

Use `--attest-target-label` to substitute a site-neutral name:

```
detect --target https://bmc-rack12.corp.internal \
       --attest-out attestation.json --attest-target-label rack12-node3
```

**Nothing here guesses which hostnames are internal.** Pattern-matching
internality would be speculation dressed as a safety feature, and it would fail
in both directions — flagging a public name and passing a private one. The
operator knows; the tool provides the override and says why it exists.

## Three invariants, and they are contracts rather than coincidences

**1. `len(evidence) == len(findings)`.** Every finding carries its measurement.
This is the artifact's whole reason to exist: a `check()` envelope renders a
finding as five keys and drops the numbers, so *`P12V_AUX` exceeds critical* is
all a bare finding says. The attestation says it was **14.8985 against a declared
12.61**. `validate-attestation` refuses a file that breaks this.

**2. `engine.schema_version` records what the judgment was made under.** A finding
is only meaningful relative to the envelope contract it was read from. Without
this, an artifact read years later cannot be placed against the engine that
produced it.

**3. `unattested` and `unread_feeds` are the boundary.** They are the things that
existed and were **not part of the judgment**. Both are lists that are normally
empty, and an absent list is not the same as an empty one — an absent list reads
as *nothing was left out*, which is a claim this format must never make silently.
`validate-attestation` refuses a file where either is missing.

## What validation does NOT check, deliberately

**That there are any findings.** A genuinely clean board produces an artifact with
`findings: []`, and that artifact is valid. This is worth stating because the rule
was originally written the other way, inline in a CI workflow that ran over a
fixture known to produce findings — and promoting that check into the shipped
validator would have meant **a clean run failing validation**, which is precisely
the inversion this project exists to prevent.

The canary still asserts findings are present, as a separate step, because that is
an expectation about *the vendored capture* rather than about the format.

**That a clean artifact carries a `boundary`.** It does not, and cannot. The
boundary is the engine's statement about what its *evidence* establishes, and it is
read off the evidence entries themselves — so a run with no findings has no
evidence and no boundary to carry. `validate-attestation` requires the boundary
only when evidence is present.

This was found by the clean-board test rather than by reading, and the first
version of the validator did reject a healthy machine over it. Recorded here
because the same trap is available to anyone writing a consumer: a rule that reads
correctly against a failing sample can still be wrong about the successful one.

## What this artifact is not

It is not a signed attestation, and nothing here establishes provenance against
tampering. The engine says so itself in the `boundary` field it stamps on every
evidence entry, and that string is carried into the artifact unedited rather than
summarised. Read it before quoting this file as an assurance artifact.
