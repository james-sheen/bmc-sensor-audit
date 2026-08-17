# S2 — the oscillation issue was not filed, and should not be

**Task as planned:** file an upstream issue for the one remaining engine gap — STABILITY
silent on a ±4000 RPM oscillation, period ≈ 4 samples, 30 × 30 s, window 15 m.

**Outcome: not filed.** Reproduced first, and the silence is correct behaviour. The
series does not match the pattern the detector is for.

## What STABILITY actually measures

From the shipped module's own docstring, `arbiter_engine/ontology/axioms/stability.py`:

```
oscillation(t, w) = (1/(w-2)) * Σ 1[ d(Si, Si-2) < eps  AND  d(Si, Si-1) > delta ]
```

It counts positions where a value is **close to two samples ago and far from one sample
ago**. That is a **period-2** detector: A-B-A-B.

The planned test series is `[9000, 9000, 1000, 1000, ...]` — period 4. At every position
`d(Si, Si-2)` is 8000, nowhere near `eps`, so no position can ever count. The score is
zero by construction, and zero is correct.

## Measured on 0.1.6

Same model, same window, same cadence, three series:

| Series | Findings | Verdict |
|---|---|---|
| `9000, 1000, 9000, 1000, ...` (period 2) | **1 — STABILITY, medium** | detector works |
| `9000, 9000, 1000, 1000, ...` (period 4) | 0 | correct: not period-2 |
| `5000 ± 50` alternating (healthy jitter) | 0 | correct: noise floor holds |

The middle row is the planned issue. The first row is the same axiom finding the thing it
exists to find. **Reporting the middle row as a defect would have described the engine as
broken on the strength of a test that could not have passed.**

## The narrower thing that is true

A fan hunting on a slower cycle — period 4, 6, 8 — is genuinely pathological, and the
engine produces **no finding and no decline**. It neither detects it nor says it did not
look for it.

That is a **feature request, not a bug**: *detect oscillation at periods other than 2, or
decline when the series has structure the period-2 metric cannot evaluate.* The second
half is the more interesting ask, and it is this engine's own idiom — the difference
between a clean result and one where nothing was testable is the property the whole
project exists to provide, and a silent zero on a period-4 square wave sits on the wrong
side of it.

Filed as an ask only if Stage 2 ever needs hunting detection. It does not: **stuck-at
carries the Stage 2 mission**, and stuck-at is verified working with a floor of about ten
samples.

## Also observed, minor

The period-2 STABILITY finding carries `detail: None`. Findings from other axioms carry a
self-explaining detail string — the `missing_property` decline, for instance, explains
what BOUNDEDNESS needed and why. Worth raising separately if it survives a check against
a wider set of STABILITY findings; not raised here because one observation of one finding
is not a pattern.

## Consequence for the plan

The Stage 2 scope statement should say oscillation detection is **out of scope because it
is not needed**, not because the engine cannot do it. Those are different sentences and
only one of them is true.
