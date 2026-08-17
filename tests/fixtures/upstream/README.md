# Vendored upstream configurations

Nine `entity-manager` configuration files, copied **verbatim** from the OpenBMC
project. Copyright 2018 Intel Corporation, Apache-2.0, licence reproduced beside
them in `LICENCE` (spelled as upstream spells it).

They exist because acceptance criterion 1 was proven by a run nobody else could
reproduce. The parser claims were measured against 247 upstream configurations on
a local checkout, by path — true, and unverifiable by any reader. These nine make
each documented claim runnable from a clone.

**Do not edit these files.** They are third-party content and their value is that
they are exactly what upstream ships. A fixture edited to make a test pass proves
only that the test passes.

## What each one is here for

| File | Reproduces |
|---|---|
| `meta/catalina/catalina_osfp.json` | **Not strict JSON** — C-style block comments. `json.load` raises. Ten of the 247 are like this, and a tool that skips them reports their sensors as undeclared rather than unread. |
| `intel/axx1p100hssi_aic.json` | **Top level is a list.** 59 of 247 are; 178 are an object. |
| `delta/delta_awf2dc3200w_psu.json` | An entry carrying a **`Labels` array that this parser does not read** — see the lead below. Kept because the lead deserves a runnable example. |
| `intel/8x25_hsbp.json` | The `$index` runtime template. |
| `meta/twinlake.json` | The `$ipmbindex` runtime template — the rarest, five occurrences in the whole corpus. |
| `meta/santabarbara/santabarbara_sitv_eth.json` | **Compound templates** — `$bus_ADC0`, `$bus_VR_P0V75SW`. These broke the first template matcher, because `_` is a word character so a greedy variable pattern swallowed the whole token and degenerated to a match-anything expression. |
| `asrock/asrock_spc621d8hm3.json` | `Status: disabled` entries — declared, deliberately off, and the case the tool exists for. |
| `meta/fbyv2.json` | **Upstream defect 1**: a `temp1` threshold named `upper critical` with `Direction: less than`. Also **one `Exposes` entry declaring several sensors** — its single `HSC` entry expands to eight, one per rail. |
| `meta/fbyv35.json` | **Upstream defect 2**, same shape, and the widest threshold-name vocabulary in the corpus. |

## What these do NOT cover

Stated because a fixture set that looks complete is worse than one that admits a
gap.

- **The `$Name` template variable is not represented.** It is exercised exactly
  twice in the entire corpus, and **every file containing it also contains real
  inventory values** — a genuine spare part number in one case. Vendoring one
  would mean either committing that value or exempting a file from the hygiene
  check, and neither is worth covering a two-occurrence variable. The parser
  handles `$Name`; nothing here proves it.
- **The corpus-wide counts are not reproduced.** 247 files, 5,496 sensors, 661
  templated names, 10,687 thresholds — those come from the full corpus and cannot
  be recomputed from nine files. The README's counts remain measurements against a
  checkout, and these fixtures prove the *findings*, not the *totals*.
- **No upstream revision is pinned.** See `NOTICE`. There was no version control
  metadata to read, so these are a snapshot of unknown upstream revision.

## One open lead, with a fixture for it

**This parser does not read an `Exposes` entry's `Labels` array.** Multi-sensor
expansion comes only from per-threshold `Label` fields — which is the opposite of
the obvious reading, and choosing a fixture on the obvious reading picked a file
that demonstrated nothing. Eight entries across four of these nine files carry a
`Labels` array that is never consulted.

**A sensor declared through `Labels` alone, with no per-rail thresholds, would be
invisible to this tool.** Whether any real board does that is unmeasured. It is
recorded here rather than resolved because measuring it means reading
`dbus-sensors` to learn what the BMC does with the key, and that is the same
unread source the upstream defect report already declares as its own limit.

## Why nine and not 247

Vendoring the whole corpus would be ~9 MB of third-party content in a repository
whose own source is under 100 KB, and it would need re-syncing forever. Nine files
carry every documented finding at 67 KB. The selection was made by measuring the
corpus for each property and taking the smallest file exhibiting it that also
passes this project's hygiene check.
