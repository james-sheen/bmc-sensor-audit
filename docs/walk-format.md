# `bmc-sensor-audit/walk/1`

One capture of what a machine actually reports: the parsed sensor set, what could
not be fetched, and when it was taken. Written by `capture --out FILE`, read back
by `coverage --walk`, `detect --walk` and `regression`, and checked by
`validate-walk FILE`.

**The versioning rule, first, because everything below depends on it.** Within
`/1` this format may gain keys and may never change the meaning of one. A meaning
change is `/2`, and so is a removal. So a downstream reader may write code that
ignores keys it does not know, and may not write code that assumes a key it does
know has kept its meaning across a major bump. The same discipline
`attestation/1` states, for the same reason: a fleet layer is downstream of two
contracts, not one.

Concretely, within `/1`:

| may happen | may not happen |
|---|---|
| a new top-level key appears | `reading` starts meaning something other than the value the machine reported |
| a new per-sensor key appears | `thresholds` changes its slot spelling |
| a new threshold slot is written under the existing `bound/level` spelling | a key present today is removed |
| a key that is optional today becomes present on every capture | an optional key becomes required of a reader |

**Absence is not a value, and this is the rule most likely to bite a new reader.**
`captured_at`, `latencies` and `fields_observed` are all missing from captures
written before those fields existed. A reader must treat absent as *unknown* and
not as a default: `fields_observed` absent means nobody compared this object
against the schema, and defaulting it to `true` would make every old capture claim
its sensors carried no undeclared properties on no evidence at all.

## Reading one without installing anything

```
bmc-sensor-audit validate-walk walk.json
```

**No engine and no hardware required**, and no dependency outside the standard
library. A walk is JSON and checking one is a Stage 1 operation. Exit `0` valid,
`1` problems listed on stderr, `2` the file could not be read.

Two flags a collector will want:

```
bmc-sensor-audit validate-walk walk.json --print-digest --require-complete
```

`--print-digest` prints the content handle described below. `--require-complete`
exits `2` when the walk did not complete: a partial capture is valid `walk/1` and
must never be used as a baseline, and the flag is how a pipeline says it needs one
that is whole. It is off by default because `capture` deliberately writes partial
captures and keeps them — they are the record of *which* subtree failed.

## What the validator checks, and what it deliberately does not

It checks malformation: the declared format, the shape of every list and object,
that each sensor carries a `name`, that readings and thresholds are numbers, and
that every threshold slot is one this build writes. It also checks the one
cross-field contradiction that matters — a sensor naming undeclared properties
while the walk says `fields_observed: false`, which is the file telling a reader
both that nobody looked and that somebody found something.

It does **not** refuse a walk carrying no sensors, and it does not refuse an
incomplete one unless asked. Both are legal captures. A validator that rejects
valid input is one people learn to route around, and they take the malformed cases
with them when they go.

## The content handle: which capture, never which machine

```
bmc-sensor-audit capture --target https://bmc --out walk.json --print-digest
  wrote 28 sensor(s) to walk.json
  digest      sha256:1f0c...
```

The digest is `sha256:` followed by the hex SHA-256 **of the file's bytes**. Any
language computes it, `sha256sum walk.json` computes it, and a recipient can check
the handle without trusting — or installing — this tool. The cost is stated rather
than hidden: rewriting the file changes the handle even where the walk is
unchanged. That is correct for a handle on a *received artifact* and wrong for one
on a walk's meaning, and this is the first.

**This is the whole of the fleet-binding surface, and it is deliberately the whole
of it.** Fleet analysis needs to know which unit a capture came from. This tool
must keep not knowing. So the binding happens outside, on content: a collector
holds `{unit_key, digest, walk_ref}` in its own envelope, and `unit_key` never
reaches anything here.

**No identity field enters `walk/1`, ever.** If a future request wants identity
just this once inside the file, the answer is the cert-generator precedent —
identity lives on the other side of the line, in the layer whose job is to name
things.

## The parse is the redaction

`capture` serialises the parsed `LiveSensor` set, never the raw Redfish payloads.
A raw chassis walk carries serial numbers, part numbers, asset tags, MAC addresses
and the machine's own inventory of who bought it; recording one is a fleet
inventory disclosure, and the natural way to build a realistic test fixture is to
commit exactly that. Capturing the parsed form keeps names, readings, units,
states and thresholds and carries none of the rest, so the safe thing is the
default thing with no flag to remember.

Undeclared properties are recorded as **names only, never values**, for the same
reason: `SerialNumber` is exactly the sort of field a strictness report is meant
to notice, and quoting what it found would publish the machine's identity while
complaining about the field.

A sensor NAME can still embed a hostname on some platforms. Read a capture before
committing it.

## Fields

Every key the writer produces today. *Stability* is the promise made within `/1`.

| Key | Type | Meaning | Stability |
|---|---|---|---|
| `format` | string | `bmc-sensor-audit/walk/1`. The contract this file claims. | Fixed; a change is a new major |
| `chassis` | array | The chassis URIs that were walked. | Present, possibly empty |
| `shapes_seen` | array | Which tree shapes answered: `sensors`, `thermal`, `power`. A shape that vanishes between two walks is a client interface going dark. | Present, possibly empty |
| `errors` | array | `[path, reason]` per failed fetch. **Non-empty means absence in this walk is unreadable**, not that sensors are missing. | Present, possibly empty |
| `captured_at` | string or null | When the walk was taken, UTC ISO 8601, stamped where the walk is taken. Null or absent on a capture written before this existed. | Absent means unknown, never now |
| `fields_observed` | boolean | Whether each sensor object was compared against the published schema at walk time. | Absent means false; see the absence rule above |
| `latencies` | array | `[path, seconds]` per fetch, in walk order. Absent on an older capture, which is a different fact from an instant response. | Absent means unrecorded, never zero |
| `sensors` | array | The parsed sensor set. Each entry is described below. | Present, possibly empty |
| `sensors[].name` | string | The name the machine reports, verbatim. **The identity**: every other field on a sensor is optional. | Required |
| `sensors[].path` | string | The Redfish URI it was read from. | Present |
| `sensors[].reading` | number or null | The value, in `units`. Null means enabled and not reading, which is a finding rather than an absence. | Present, may be null |
| `sensors[].units` | string or null | As the machine reported them. | Present, may be null |
| `sensors[].state` | string or null | `Status.State`. Absent state is read as enabled: the schema makes it optional and most implementations omit it on healthy sensors. | Present, may be null |
| `sensors[].health` | string or null | `Status.Health`. | Present, may be null |
| `sensors[].shape` | string | Which tree it was read from: `sensors`, `thermal`, `power`. | Present |
| `sensors[].resource` | string | The Redfish schema type it was read as — `Sensor`, `Fan`, `Voltage`, `PowerSupply`, `Temperature`, `PowerControl`. Property strictness is judged against this. | Present |
| `sensors[].thresholds` | object | `"bound/level"` to number, where bound is `upper` or `lower`. | Present, possibly empty |
| `sensors[].undeclared` | array | Property NAMES this object carried that the published schema for its resource type does not declare. **Written only when non-empty** — the walk-level `fields_observed` flag is what separates *nothing undeclared* from *nobody looked*. | Absent means none found |
