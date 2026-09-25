# Burn-in: how many walks, at what interval, and what the answer means

A burn-in station runs a board for hours and wants one question answered at the
end: **did every sensor stay alive the whole time?** This is the recipe for
asking it, and the arithmetic behind the number of walks — measured against
`arbiter-engine` 0.1.7, not assumed.

## The short answer

**Take at least ten walks. The interval does not change that number.**

Everything below is why, and what the interval *does* change.

## The floor is ten samples, and the window is a ceiling

Liveness is `STABILITY`: a reading the model says should vary and which has not.
The engine will not judge it below **ten observations inside the indicator's time
window** — under that it declines `insufficient_samples`, which this tool reports
rather than hides.

The window is the part nobody expects. It is not *how much history to consider*;
it is a **hard cap on how many samples can ever count**, and if that cap sits
below the floor the axiom is dead — permanently, while reporting
`insufficient_samples`, which reads exactly like still warming up.

Measured on 0.1.7, with observations declared one minute apart:

| Indicator window | Most samples that can ever be in-window | Ten-sample floor reachable |
|---|---|---|
| `10m` | 9 | **no — 100 walks still declined** |
| `15m` (what this tool generates at a one-minute grid) | 14 | yes, from the tenth walk |
| `60m` | 59 | yes |

The `10m` row is the one worth staring at. A hundred walks, a completely frozen
sensor, and the answer is still *not enough data* — because the in-window count
saturates at `window ÷ interval − 1` and that is nine.

So the usable band is **10 walks minimum, with headroom to 14**. Past 14 the
oldest samples fall out of the window and the newest 14 are what gets judged,
which is fine: 14 is still above the floor. A test pins that relationship
(`tests/test_burn_in_cadence.py`), because a future edit narrowing the window to
make the check *more responsive* would silently switch liveness off across every
platform.

## The window is a number of samples, not a number of minutes

**This page used to say the station's interval was not in the arithmetic, and
that it was deliberate.** It was not deliberate so much as unavoidable at the
time, and the sentence that explained it named the real problem:

> Declaring the real interval instead would make the window meaningful and
> switch liveness off on any station walking more slowly than about a hundred
> seconds, which is most of them.

That is true, and it was measured: at a five-minute cadence a fifteen-minute
window holds two samples, and two completely frozen sensors over forty walks
produce no finding at all. The branch that was missing is the third one — tell
the engine the real interval **and scale the window with it**. A window is a cap
on countable samples, so its only meaningful unit is the collector's own step,
and fifteen minutes was fifteen of those steps all along with one of the three
terms left out.

`presence-audit` 0.1.10 does both. The window is generated as **fifteen
collection intervals**, so:

| Cadence you declare | Window generated | Samples it holds |
|---|---|---|
| none declared | `15m` | 14 |
| 60 s | `15m` | 14 |
| 300 s | `75m` | 14 |
| 900 s | `225m` | 14 |

**A board that declares no cadence is unaffected**, and that is not a hope: the
default grid is still sixty seconds and `15m` is still the string generated, so
every model this tool has ever produced is unchanged.

**The cadence is declared in `--supplemental`, as `sampling_interval_s`,** and it
is required as soon as a coupling is declared — a coupling's propagation delay
has to land on the collection grid or no pair of readings can be aligned. If you
declare no coupling you need not declare a cadence, and the paragraph below is
then the whole story.

## What the interval decides

It decides what the answer **means**, and the tool cannot know that for you:

- Ten walks five seconds apart: this value did not move in 45 seconds.
- Ten walks ten minutes apart: this value did not move in an hour and a half.

Same finding, two very different claims about the board. The capture's
`captured_at` stamp is the record of which one you made — it is why walks carry a
capture time at all, and `detect` prints the span the walks actually cover.

## Choosing the interval

**Walk more slowly than the BMC updates its sensors.** This is the one way to
produce false frozen findings on a healthy board: poll a temperature every second
when its own refresh period is five, and ten walks legitimately return one value.
The sensor is fine; the cadence asked a question it could not answer.

The refresh period is platform-specific and this project does not have a number
for yours. Find it the way you would find any other: take a burn-in run, then read
the report. **If a large fraction of the board is flagged frozen at once, suspect
the cadence before the hardware** — a board does not usually lose forty sensors
simultaneously, and one that did would show it in Stage 1 presence findings too.

A minute between walks is a reasonable starting point for a station that has no
other constraint. It is also, not coincidentally, what the feeder declares.

## The recipe

Capture across the burn-in window, then judge the run in one pass:

```
# during burn-in, on the station's timer -- at least ten times
bmc-sensor-audit capture --target https://<bmc> --insecure --out walk-$(date +%s).json

# at the end
bmc-sensor-audit detect --config <configs> $(printf -- '--walk %s ' walk-*.json)
```

`--walk` is repeatable and reads oldest first. It does not have to be typed in
order: every capture carries its own timestamp, and `detect` puts them in
chronological order and says when it had to. A shell glob sorts lexically, in
which `walk10` precedes `walk9`, and the last walk supplies every current
reading — so this is not a presentation detail.

If the captures were taken by something that does not stamp them, the order you
supply is the order used and the run says so. Fix the capture rather than the
argument order.

## What a clean burn-in does and does not establish

It establishes that every declared sensor was present, enabled, reading, and
moving across the run.

It does not establish that a sensor which is *going* to fail would have been
caught: a value moving because the board is heating up under load is a weaker
demonstration of liveness than a value moving on a thermally settled board, and
this tool cannot tell those apart. And the honest limit stated everywhere else in
this repository applies here too — **no real BMC has yet been watched going quiet
by itself.** The stuck-at pathway is proven against firmware readings under
ground truth somebody controlled, which is not the same claim.
