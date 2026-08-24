# S3 — what a full-corpus run costs, measured rather than extrapolated

**Question.** The parsed corpus grew when the reader started taking an entry's rail
set from its `Labels` array. Does a `detect` run over the whole vendored corpus still
finish in a time a CI gate can live with, and where does the cost sit?

**Measured 2026-08-20** against `arbiter-engine` 0.1.7 — the version the declared pin
`>=0.1.6,<0.2` resolves to today — on the development sandbox. Five rounds.

> **Note added 2026-08-24.** The pin has since been re-derived to `>=0.1.8,<0.2`,
> because the canary showed 0.1.6 failing eleven of its assertions and 0.1.7 two.
> The sentence above is left as written: it records the conditions this measurement
> was taken under, and a dated measurement that quietly acquires today's pin is a
> measurement nobody can reproduce.

## Answer

**0.35 s end to end.** Nothing here needs attention, and the shape is worth recording
because it is not the shape the extrapolation predicted.

| Stage | Median | Min | Max | Spread |
|---|---:|---:|---:|---:|
| Model load | 0.252 s | 0.221 s | 0.267 s | 0.046 s |
| Feed (entities + 12 observations each) | 0.028 s | 0.020 s | 0.049 s | 0.029 s |
| `check()` | 0.074 s | 0.065 s | 0.092 s | 0.027 s |
| **Total** | **0.35 s** | | | |

`checked` reports **360 invariants over 180 entities** — two axioms on the one
indicator each modelled sensor carries.

## The population is 180, not 377

The corpus declares **377** sensors. **180** reach the model, and the difference is
not loss — it is the manifest's whole job. Templated names, non-sensor `Type`s and
sensors with nothing to bound against are excluded *with a reason recorded*, and the
largest bucket is `no_thresholds`, which grew when the rail fix made rails visible
that no threshold guards.

**So a scale claim has to name which number it is about.** *377 entities* would be an
overstatement of what the engine is asked to do by more than a factor of two, and
`check()` cost is driven by the entities fed, not by the declarations read.

## Model load dominates, and that was not the prediction

The engine's published cost table falls to about **127 µs per evaluation at a
thousand entities**; measured here, at 180 entities, it is **205 µs**. Those are
consistent — a falling per-evaluation curve is higher at the small end — but the
useful finding is elsewhere: **`check()` is not the expensive stage at this scale.
Model load is, at roughly 3.4× the check.**

That inverts the thing worth watching. The earlier granularity measurement recorded
`check()` as superlinear in entity-type count and flagged it as the risk at 10⁴. It
still is, eventually. At the scale this tool actually meets — one platform's
declaration — the cost is paid parsing a generated YAML model into the engine, once,
and a consumer running repeated checks against one loaded session pays it once.

## The engine talks to stderr at this scale

A full-corpus run where many sensors breach emits a stream of
`FireFrequencyTracker: high fire rate for (BOUNDEDNESS, unknown)` warnings — roughly
fifty lines in the run above, on a `warn_rate_per_hour=100` default. It is the engine
noticing that one axiom is firing a lot, which is correct behaviour and reasonable
advice at the scale it was written for.

It is recorded here because a consumer meeting it for the first time will read it as
a defect in their configuration, and because it scales with corpus size rather than
with anything the operator did wrong. Not acted on: it goes to logging rather than to
the report, it does not affect the verdict, and suppressing another project's
diagnostic is not this repository's call to make.

## What is pinned, and what is not

The canary asserts the whole path completes **under ten seconds**, which is roughly
thirty times the measured median. That ratio is deliberate. The spread above is 0.027 s
on a 0.074 s check — about a third of the median — so a tight bound would fail on
ordinary jitter, and a row that goes red for a legitimate reason every few runs is one
people learn to skip. The ceiling is there to catch an order-of-magnitude regression
from an engine release inside the pin, which is the failure it can actually see.

**The numbers in the table are not pinned.** They are a measurement on one machine on
one day, and a test asserting them would be measuring this sandbox. Re-run the probe
on any engine bump inside the pin; the canary covers the catastrophe, this document
covers the shape.
