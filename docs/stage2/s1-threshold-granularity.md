# S1 — threshold granularity: per-entity overrides vs one type per sensor

**Question.** An `arbiter-engine` indicator carries thresholds on the entity *type*.
Every BMC sensor has its own values. Either the generator emits one entity type per
sensor — hundreds to thousands of types per platform — or it emits a single `Sensor`
type and attaches thresholds per entity.

**Measured against `arbiter-engine` 0.1.6** (the version this project pins), 2026-08-17.

## Answer

**One entity type per sensor.** The per-entity path is not available for the axiom that
matters, and the type-per-sensor cost is small enough to be uninteresting.

## Why the per-entity path is unavailable

`arbiter_engine.axiom_thresholds.resolve_axiom_threshold(entity, indicator, axiom,
fallback, *, bound=...)` exists and its docstring says it returns *the per-entity override
for `(indicator, axiom)`*, read from `entity.properties[__cd508_axiom_thresholds__]`.

That is true of the helper. It is not true of the engine, because **BOUNDEDNESS never
calls it.**

| Module | Calls `resolve_axiom_threshold` |
|---|---|
| `ontology/axioms/homeostasis.py` | yes — twice |
| `twin/traverser.py` | yes — twice |
| `twin/monte_carlo_predictor.py` | imports it |
| **`ontology/axioms/boundedness.py`** | **no — a comment mentioning it, and no call** |

Confirmed behaviourally rather than by reading. Two entities of one type whose indicator
declares warning 10.0 / critical 20.0, both reading 5.0, one carrying an override of
warn 1.0 / critical 2.0:

```
findings: []
```

The override entity should have breached its own critical at 5.0 > 2.0. It did not: the
type-level threshold was used and the override was ignored. **A helper that exists,
documents per-entity semantics, and is not wired into the checker you need reads exactly
like a working feature until you test the verdict rather than the function.**

HOMEOSTASIS does honour it, so the mechanism is real — just not on the axiom the
generator depends on. If BOUNDEDNESS is ever wired to it, this decision should be
revisited; that is the off-ramp.

## What type-per-sensor actually costs

Synthetic, one indicator per type, two axioms (`BOUNDEDNESS` + `STABILITY`), one entity
per type with twelve in-window observations:

| Types | Model load | Feed | `check()` | Total | `checked` |
|---:|---:|---:|---:|---:|---|
| 83 | 0.130 s | 0.009 s | 0.029 s | **0.17 s** | 166 invariants / 83 entities |
| 250 | 0.379 s | 0.026 s | 0.128 s | **0.53 s** | 500 / 250 |
| 500 | 0.687 s | 0.061 s | 0.351 s | **1.10 s** | 1000 / 500 |
| 1000 | 1.661 s | 0.131 s | 1.424 s | **3.22 s** | 2000 / 1000 |

83 is the real number for the nine vendored configurations — 94 declared sensors, 83
after excluding templated names.

**Model load is linear. `check()` is not**: 12× the types costs 49× the check. At 10³ that
is 1.4 s and irrelevant; extrapolated to 10⁴ it is roughly a minute, which would matter.
Nothing in the corpus suggests a single platform declares 10⁴ sensors — the largest
vendored configuration is well under that — but the superlinearity is the thing to watch,
not the absolute number, and it is why this table records the shape rather than a single
verdict.

## Consequences for the generator

- Emit one entity type per concrete sensor; name it from the sensor name, sanitised.
- **Templated names are excluded before generation**, not filtered afterwards: 11 of the
  94 vendored declarations carry runtime variables, and a `$`-name fed to the engine would
  become an entity type nothing can ever match.
- The manifest must map generated type name back to original sensor name, because the
  sanitisation is lossy and every finding will name the sanitised form.
- Re-run this probe on any engine bump inside the pin range. The canary (S4) covers the
  behavioural pillars; this covers the cost.
