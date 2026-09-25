# Vendored fan-control configuration — Ampere Mt. Jade

Three **phosphor-fan-control** configuration files for the Ampere Mt. Jade
platform, copied **verbatim** from the OpenBMC project's Ampere layer:

    https://github.com/openbmc/openbmc
    meta-ampere/meta-jade/recipes-phosphor/fans/phosphor-fan/

**Pinned to `9a2f15583ae35eeb06852b4f825b440653323338`**, the last upstream
commit to touch any of the three. Every file here is byte-identical to its blob
at that revision, compared by git object hash rather than by eye:

| file | blob |
|---|---|
| `events.json` | `537a1ef3defe03326f18928a12277d2c1949ff84` |
| `zones.json` | `d8ec74f5b20937b74aff5e43b51ec08c45f94f50` |
| `groups.json` | `fdfe57d82d6fff4afdc1420153972dc9b447ec58` |

The layer states its own licensing, and that statement is reproduced here
unmodified as `LICENSE`, with the two texts it names beside it
(`COPYING.apache-2.0`, `COPYING.MIT`). **Do not edit these files.** Their
value is that they are exactly what the platform's firmware ships.

## Why they are here

They are the basis for the one coupling `examples/supplemental/ampere-mtjade.json`
declares: **`TS4_Temp` drives `FAN3_1`**. A coupling's `basis` has to say what
makes one reading drive another, and these files say it in the vendor's own
configuration. A test reads every clause of that basis back out of them, so the
basis cannot drift from its source without going red.

- `groups.json` puts `TS4_Temp` in the group `zone0_ambient`, and FAN3 to FAN8
  in `air_cooled_zone0_fans`.
- `zones.json` declares one zone with `increase_delay: 5` and
  `decrease_interval: 30`.
- `events.json` event `target_mapping_from_TS_temp` maps the ambient group's
  temperature to the zone's fan target through a 21-point table that never
  decreases: 38 of 255 at 10 C, 89 of 255 at 50 C.

Two upstream `phosphor-fan-presence` documents define what the configuration
means, and they are cited rather than vendored. Both are documentation, and the
coupling rests on the configuration:

- `docs/control/events.md` at `9f2b5056da97b2d64bbee0e41327cf977f533bc7`:
  *"If there are more than one event using this action, the maximum speed derived
  from the mapping of all groups will be set to the zone's target."*
- `docs/control/zones.md` at `64fb88c1bfd81e970b15f13938d59fc04d030b0f`: the two
  delays throttle target increases and decreases.

## What they do NOT establish

A basis that looks complete is worse than one that admits a gap.

- **No gain.** The table maps degrees to a PWM target out of 255. The reading a
  collector sees is a tachometer in RPM, and how a PWM target becomes RPM is
  the fan's curve, which is not in these files. So the coupling withholds its
  gain (`estimate`), and the engine fits one from history.
- **Not a line.** The table is flat at 38 up to 18 C. Above that it rises at
  anywhere from 0.75 to 3 counts per degree, shallowest between 24 and 40 C.
  And the zone takes the MAXIMUM of six mappings. The others are the SoC, DIMM,
  OCP and two NVMe groups. So `TS4_Temp` moves these fans only while its own
  mapping is the highest one requested. A fitted gain is an average slope over
  whatever regime the history covers, and a history with a hot SoC will show a
  fan that does not follow the ambient reading at all.
- **Not a thermal claim.** This is the firmware's CONTROL LAW: which reading
  sets the fans. How the fans then move the temperatures is airflow physics,
  and nothing here states it. That is why the coupling runs from the inlet-air
  reading, which a fan cannot change much, to the fan. Running it the other way
  would declare a physical effect with no document behind it.
- **Timing only down to one collection step.** The zone follows a rise within
  5 s and a fall within 30 s. At a collection cadence of 30 s or longer, the
  whole response lands inside one step. So the coupling is declared as a step
  with no delay at the example's 60 s cadence, and it would be the wrong shape
  for a collector walking faster than that.
