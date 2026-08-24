# Declaration sources: `pdr/1` and `fleet-baseline/1`

The manufacturer's entity-manager files are the declaration this tool was built
around. They are not the only place an expectation can come from, and on some
platforms they do not cover the hardware anyone is worried about.

**Why this seam exists.** On NVIDIA-managed platforms (HGX and GB200-class) the GPU
and HMC sensors arrive as *runtime self-description* — PLDM PDRs and NSM discovery,
projected to Redfish by the BMC — and typically have no entity-manager entry at all.
Today they land in coverage's reverse direction, **machine has, declaration
doesn't**, which is true and unhelpful: nothing about them can ever be a regression,
because nothing ever expected them.

So they arrive as explicit, versioned, labeled inputs. The family precedent is to
add declaration sources as formats of their own and never to replace the
manufacturer's file.

## Precedence, and it is pinned by a test

| rank | source | what it covers |
|---|---|---|
| 1 | **entity-manager** (`--config`) | the manufacturer's declaration. Wins wherever it declares, always |
| 2 | **`pdr/1`** (`--declaration`) | a reviewed snapshot of one platform's discovered inventory. Covers what entity-manager does not |
| 3 | **`fleet-baseline/1`** (`--declaration`) | derived from a fleet. The explicit last resort |

Nothing ever replaces an entity-manager entry, and nothing merges two entries into
one: the loser is dropped whole, so a threshold never arrives from a different
source than the sensor it bounds. Precedence is applied over the matcher's
normalisation, not over the raw string — two sources spelling one sensor
`HGX_TEMP0` and `HGX TEMP0` are declaring the same sensor, and keeping both would
expect it twice and report one permanently absent.

## The circularity hazard, which is the founding problem of this tool one door over

A declaration derived from a walk is only as good as the machine it was walked
from. **A walk of an unprovisioned board yields an empty declaration that reads
perfectly healthy against every other unprovisioned board**, and no check inside the
file can tell that from a good one.

Two rules follow, and they are the whole of the design.

### A candidate refuses to be consumed

The tool will happily *emit* a `pdr/1`:

```
bmc-sensor-audit declare --from-walk walk.json --candidate \
    --platform HGX-H100 --firmware 1.03.05 --out hgx.json
```

What it writes carries `"reviewed": null`, and `coverage`, `detect` and `declare`
**refuse** a source without a complete reviewed marker — loudly, naming the file,
before a single sensor is compared. Adding the marker is the review:

```json
"reviewed": {"by": "<name>", "on": "<date>"}
```

Both halves are required. A marker naming a reviewer and no date, or a date and no
reviewer, is the shape of somebody clearing the gate rather than passing it, and is
refused like an absent one.

**The rule keys on the marker's presence, never on a `candidate: true` flag.** A
flag can be deleted while the review never happens; a marker can only be added by
someone writing their own name into it, so there is nothing else to forge.

The emitter refuses three walks outright, because no review could repair them: one
that did not complete (it would bake the transport failure in as an expectation of
fewer sensors), one that reports nothing (an empty declaration reads clean against
every machine), and one carrying no capture time. Nothing stamps a snapshot with
*now* — that would date it to the moment somebody converted it.

### Every run that used one says so

```
  Declared partly from sources other than entity-manager:
    2 sensor(s) from bmc-sensor-audit/pdr/1, platform HGX-H100, firmware 1.03.05,
    captured 2026-08-24T09:00:00+00:00, reviewed by an operator on 2026-08-24 -- hgx.json
```

Printed above the counts, because a reader decides how much to believe a number
before they read it rather than after. Carried on the report itself rather than
passed to each renderer, so a new output format cannot be added without the
provenance coming with it. In `--json` it is `declaration_sources`, present only
when one was used, carrying both the fields and the rendered sentence.

A `fleet-baseline/1` adds a second line to its own provenance in every report:

```
    This declaration is derived from a fleet, not from this platform's
    manufacturer. It is the explicit last resort.
```

**Silence cannot impersonate the manufacturer.** That is the point of the whole
block.

## The `Type` filter does not apply to these sources

`coverage` sets aside declarations whose entity-manager `Type` does not produce a
reading — PID loops, EEPROMs, firmware blobs, muxes, GPIO presence detectors. That
filter is a fact about entity-manager, not about declarations in general.

These sources record only things that were *reading*. Applying the filter to one
would classify every entry as an unrecognised Type, so a GPU sensor that stopped
reporting would be counted, printed, and **never once fail a gate** — a feature
built to make those sensors gateable making them ungateable, in a way that reads
exactly like working. So a source declares whether the filter applies, and these
two say it does not.

## Fields

Every key the reader consumes. **Unknown keys are ignored, by the `/1` rule** — a
producer may carry whatever else it needs, and `fleet-baseline/1` is defined by the
downstream fleet layer rather than here. What is defined here is the subset this
reader consumes.

| Key | `pdr/1` | `fleet-baseline/1` | Meaning |
|---|---|---|---|
| `format` | required | required | `bmc-sensor-audit/pdr/1` or `bmc-sensor-audit/fleet-baseline/1` |
| `platform` | required | required | What this declaration is scoped to. A declaration scoped to nothing in particular is one nobody can tell was pointed at the wrong machine |
| `firmware` | **required** | optional | Discovered inventory moves with firmware, so a PDR snapshot that does not say which firmware produced it cannot be checked against anything. A fleet baseline spans firmware levels by construction |
| `captured_at` | **required** | optional | Which moment this snapshot is of |
| `derived_from` | ignored | **required** | What the baseline was derived from. A downgrade, and this is the whole of what a reader has to judge it by |
| `reviewed` | required | required | `{"by": ..., "on": ...}`. Absent or half-filled makes the file a candidate, and candidates are refused |
| `sensors` | required | required | Non-empty. Each entry needs a `name`; `thresholds` is optional |
| `sensors[].name` | required | required | The name the machine is expected to report |
| `sensors[].thresholds` | optional | optional | `"upper/<level>"` or `"lower/<level>"` to a number — the same slot spelling `walk/1` writes, because a `pdr/1` is normally derived from a walk and a second vocabulary is how two records of one fact come to disagree |
| `note` | ignored | ignored | Informational. The emitter writes one saying the file asserts nothing |

## A worked example

```
# 1. capture, and derive a candidate from it
bmc-sensor-audit capture --target https://bmc --out hgx-walk.json
bmc-sensor-audit declare --from-walk hgx-walk.json --candidate \
    --platform HGX-H100 --firmware 1.03.05 --out hgx.json

# 2. read it against the platform's documentation, then add the reviewed marker

# 3. gate on it
bmc-sensor-audit coverage --config /usr/share/entity-manager/configurations \
    --walk hgx-walk.json --declaration hgx.json
```

Step 2 is the product. Steps 1 and 3 are plumbing around it.
