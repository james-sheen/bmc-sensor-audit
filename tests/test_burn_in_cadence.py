"""The burn-in arithmetic: enough walks, and a window that can hold them.

**The load-bearing test is `test_the_window_can_hold_more_samples_than_the_floor`,**
and it exists because of a measurement that surprised the author. The indicator's
time window is not *how much history to consider*. It is a hard ceiling on how
many observations can ever be counted, and if that ceiling sits below the
ten-sample floor the axiom is dead -- permanently, while declining
`insufficient_samples`, which reads exactly like still warming up.

Measured on 0.1.7: a `10m` window with observations declared a minute apart
declines a completely frozen sensor after **a hundred** walks. Nine samples is all
that window can ever hold.

**THE THIRD TERM WAS HIDDEN UNTIL `presence-audit` 0.1.10.** This file has always
said the three constants are related. Two of them were named and the third --
the collection cadence -- was the fixed 60 seconds the feeder stamped on every
observation whatever a supplemental file declared. So `window: 15m` held
fourteen samples against a floor of ten because 15 minutes is fifteen of those
sixty-second steps, and neither the window nor this test said so.

When the feeder started using the DECLARED cadence, the divisor moved and
nothing else did: at a five-minute walk a fifteen-minute window holds TWO.
Measured end to end -- two completely frozen points over forty walks produced no
finding at all. The window is now generated as a number of SAMPLES, which is the
only unit it ever meant, and at sixty seconds it still renders `15m` exactly.

**AND THIS FILE USED TO GREP THE FEEDER'S SOURCE** for `interval_seconds=60.0`,
on the stated grounds that the value was a keyword argument and not a constant
it could import. That reason is gone: it is `DEFAULT_SAMPLE_INTERVAL_S`, and the
grid a run actually used is reported on the feed result. A check that reads text
to prove something about code is a defect this project has now hit five times;
where there is a value to import or a result to read, that is the thing to ask.
"""

from __future__ import annotations

import re

from presence_audit import feeder, generator


def _window_seconds(window: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", window)
    assert match, f"the generated window {window!r} is not a duration this parses"
    value, unit = float(match.group(1)), match.group(2)
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def test_the_default_grid_is_still_the_one_this_file_assumes():
    """Imported, not grepped. If the default moves, the arithmetic below is
    about nothing -- and a premise that silently stopped holding is worse than
    no test."""
    assert generator.DEFAULT_SAMPLE_INTERVAL_S == 60.0


def test_the_generated_window_is_unchanged_at_the_default_grid():
    """The compatibility claim, stated where an operator's boards live. Every
    model this tool has generated carries `15m`, and a board being fed at sixty
    seconds must see no change from the cadence work at all."""
    assert generator.window_for(generator.DEFAULT_SAMPLE_INTERVAL_S) == "15m"


def test_the_window_can_hold_more_samples_than_the_floor():
    """The load-bearing test. See the module docstring.

    In-window capacity is `window / interval - 1`: the engine lays the series out
    backwards from now at the declared spacing, so the oldest of N samples sits at
    `N * interval` and falls outside a window of exactly that length.

    Checked at every cadence a real collector plausibly walks at, because the
    fixed-window version passed this at sixty seconds while being dead at five
    minutes, and sixty seconds was the only number it was ever asked about.
    """
    for interval in (10.0, 30.0, 60.0, 300.0, 900.0):
        window = generator.window_for(interval)
        capacity = _window_seconds(window) / interval - 1
        assert capacity >= feeder.STUCK_AT_SAMPLE_FLOOR, (
            f"a {window} window at {interval:g}s per sample "
            f"holds {capacity:g} observations, and STABILITY needs "
            f"{feeder.STUCK_AT_SAMPLE_FLOOR}. Liveness would decline "
            f"insufficient_samples forever, at any number of walks")


def test_the_documented_recipe_matches_the_floor():
    """The burn-in document tells an operator a number. It has to be this one."""
    from pathlib import Path

    text = (Path(__file__).parent.parent / "docs" / "burn-in.md").read_text()
    assert "at least ten walks" in text.lower()
    assert str(feeder.STUCK_AT_SAMPLE_FLOOR) in text


def test_the_span_of_a_run_is_reported_and_not_guessed():
    """`frozen` alone does not say whether the value held still for a minute or a
    shift: the engine is told each sample is one COLLECTION INTERVAL old, and a
    supplemental file declaring that cadence is optional. Only the capture stamps
    carry the real elapsed time, so a run whose walks are unstamped must decline
    to state a span rather than printing a plausible one."""
    from bmc_sensor_audit.cli import _walk_span
    from bmc_sensor_audit.inventory.redfish import Walk

    stamped = [Walk(captured_at="2026-08-20T09:00:00+00:00"),
               Walk(captured_at="2026-08-20T11:30:00+00:00")]
    assert "2:30:00" in (_walk_span(stamped) or "")

    mixed = [Walk(captured_at="2026-08-20T09:00:00+00:00"), Walk()]
    assert _walk_span(mixed) is None, (
        "an unknown span must not render as 0:00:00, which is the one answer "
        "that is certainly wrong")
    assert _walk_span([Walk(captured_at="2026-08-20T09:00:00+00:00")]) is None
