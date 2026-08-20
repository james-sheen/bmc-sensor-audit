# Vendored upstream configurations

Thirteen `entity-manager` configuration files, copied **verbatim** from the OpenBMC
project. Copyright 2018 Intel Corporation, Apache-2.0, licence reproduced beside
them in `LICENCE` (spelled as upstream spells it).

**Pinned to `0ada048303bb007c9d7ec3a6a90433169f05dd99`.** Every file here is
byte-identical to that revision.

They exist because acceptance criterion 1 was proven by a run nobody else could
reproduce. The parser claims were measured against a local checkout, by path.
True, and unverifiable by any reader. These thirteen make each documented claim
runnable from a clone — and the three `meta/bletchley/` files go further: paired
with `tests/fixtures/walk_qemu_bletchley.json` they make the whole coverage diff
reproducible from a clone, declaration and machine both.

**Do not edit these files.** They are third-party content and their value is that
they are exactly what upstream ships. A fixture edited to make a test pass proves
only that the test passes.

## Why the pin exists

The first version of this directory was **not** pinned, because the checkout it
was copied from carried no version-control metadata and no revision could be
named. That looked like a documentation gap. It was a live defect, and it showed
within a day:

- Two of the nine had already been **renamed upstream** — `delta_awf2dc3200w_psu`
  and `asrock_spc621d8hm3` both lost their vendor-name prefix. A reader comparing
  against upstream would have found them missing.
- Every corpus **count** had moved: 247 configurations became 349, 5,496 declared
  sensors became 8,684, 10,687 thresholds became 15,860.
- Every **structural** claim survived exactly: ten files carry block comments, the
  threshold-name vocabulary has fifteen spellings, one entry declares 33 rails,
  `Direction` takes two values, and both upstream defects are still present.

That split is the useful part. Counts drift; shapes do not. **An unpinned snapshot
of third-party content does not stay a snapshot — it becomes a claim about a
revision nobody can identify.**

## What each one is here for

| File | Reproduces |
|---|---|
| `meta/catalina/catalina_osfp.json` | **Not strict JSON** — C-style block comments. `json.load` raises. Ten of the 349 are like this, and a tool that skips them reports their sensors as undeclared rather than unread. |
| `intel/axx1p100hssi_aic.json` | **Top level is a list.** 75 of 349 are; 264 are an object; the remaining 10 are the JSONC files above. |
| `intel/8x25_hsbp.json` | The `$index` runtime template. |
| `meta/twinlake.json` | The `$ipmbindex` runtime template — the rarest, and the only hygiene-clean file at the pin that carries it. |
| `nvidia/cx7_mezzanine_module.json` | **Compound templates** — `$bus_`-prefixed names. These broke the first template matcher, because `_` is a word character so a greedy variable pattern swallowed the whole token and degenerated to a match-anything expression. |
| `asrock/spc621d8hm3.json` | `Status: disabled` entries — declared, deliberately off, and the case the tool exists for. |
| `delta/awf2dc3200w_psu.json` | A **`Labels` array with a threshold on none of its rails**, every one of them named by a runtime template. Vendored as the runnable example of a lead that has since been answered — see below. |
| `ampere/mtjade.json` | A **real input/output power pair on named rails** — `PSU0_PINPUT` and `PSU0_POUTPUT`, declared through `Labels`, bounded by nothing, templated in neither name. The CONSERVATION specimen. |
| `nvidia/cx7_mezzanine_module.json` | Also **`Labels` and `Name1` on one entry** — the ambiguous overlap, reported rather than guessed. |
| `meta/fbyv2.json` | Also **multi-channel parts**: three TMP421 entries whose remote input is named `Name1`, which this reader discarded until a capture against real firmware reported them. |
| `meta/fbyv2.json` | **Upstream defect 1**: a `temp1` threshold named `upper critical` with `Direction: less than`. Also **one `Exposes` entry declaring several sensors** — its single `HSC` entry expands to eight, one per rail. |
| `meta/fbyv35.json` | **Upstream defect 2**, same shape, and the widest threshold-name vocabulary in the corpus. |
| `meta/bletchley/bletchley_baseboard.json` | The **declaration half of the end-to-end reproduction** — 13 `Labels` entries, and the two `Name1` TMP421 channels that real firmware reported while this reader was discarding them. |
| `meta/bletchley/bletchley_chassis.json` | A record probing on `FOUND('Bletchley Baseboard')` — a board that exists only because another one does. |
| `meta/bletchley/bletchley_frontpanel.json` | **`NameHumidity`**, the quantity-named channel spelling. Later revisions renamed it to `Name1`, so a reader written against a current checkout looks complete and still drops it here. |

## What these do NOT cover

Stated because a fixture set that looks complete is worse than one that admits a
gap.

- **The `$Name` template variable is not represented.** It is exercised barely at
  all across the corpus, and **every file containing it also contains real
  inventory values** — a genuine spare part number in one case. Vendoring one
  would mean either committing that value or exempting a file from the hygiene
  check. The parser handles `$Name`; nothing here proves it.
- **The corpus-wide totals are not reproduced.** 349 files, 8,809 sensors, 709
  templated names, 15,860 thresholds — those come from the full corpus and cannot
  be recomputed from thirteen files. These fixtures prove the *findings*, not the
  *totals*.

## A lead that was answered, and the two rounds it took

**The report:** *this parser never consults an entry's `Labels` array, and a sensor
declared through `Labels` alone would be invisible to it.* Correct on both counts.

**Round one narrowed it and should not have.** The array was read for exactly one
purpose — an entry carrying both a `Labels` list and several `Name<n>` channels is
ambiguous, because the list can select which channels exist at all, and the answer
belongs to the device class rather than to this file. That case is reported as an
anomaly instead of being guessed at, and `nvidia/cx7_mezzanine_module.json` carries
two of them. That part stands.

Expansion was left alone, and this file recorded the remainder as an open question
*whether any real board does that is unmeasured*. A test was written asserting the
narrow behaviour, and **a correct outside finding became documented behaviour.**

**Round two measured it.** Rail expansion is now driven by the `Labels` array that
declares the rail set; per-threshold `Label` still contributes any rail the array
omits, because the two disagreeing is a fact about the configuration rather than a
reason to trust the shorter list. See the numbers below.

**A sensor declared through `Labels` alone, with no per-rail thresholds, was
invisible to this tool — and it is measured now.** This section said *whether any
real board does that is unmeasured*, and deferred measuring it on the grounds that
it meant reading `dbus-sensors` first.

It did not. Counting the vendored files answers it: across these thirteen,
`Labels` arrays declare **149 rails and 34 carry a threshold**. The rail set is now
taken from `Labels`, which declares it, rather than from the thresholds, which are
a proxy for it. The other 115 were never constructed, so nothing expected them,
and a rail nothing expects can never be reported absent.

The reading of `dbus-sensors` that was treated as a prerequisite is a separate and
narrower question — what the BMC *names* such a rail. `<label>_Name` answers it
wherever an entry supplies one (72 of the 149 here); the rest fall back to
`NAME:LABEL`, which is the convention this tool already used for rails it could
see.

## Why thirteen and not 349

Vendoring the whole corpus would be megabytes of third-party content in a
repository whose own source is under 100 KB, and it would need re-syncing forever.
Twelve files carry every documented parser finding, and the bletchley three among
them add the only end-to-end reproduction of a real diff. The selection was made by
measuring the corpus at the pin for each property and taking the smallest file
exhibiting it that also passes this project's hygiene check.

**`ampere/mtjade.json` is the thirteenth**, and it is here for a Stage 2 reason
rather than a parser one: it declares `PSU0_PINPUT` and `PSU0_POUTPUT` — a real
input/output power pair, on named rails, with no runtime template in either name —
which is what a CONSERVATION check needs and what nothing else vendored here
supplies. The Delta PSU declares `pin` and `pout1` too, but every name it exposes
is templated, so no live sensor can ever match one.
