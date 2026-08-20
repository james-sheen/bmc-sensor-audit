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

This build generates `window: 15m` and feeds observations at 60 seconds, which
holds fourteen against a floor of ten. The margin is four. Nothing else in the
tree connects those three constants, so an edit narrowing the window to make
liveness *more responsive* would switch it off on every platform and no test would
notice. This is that test, and it asserts the relationship rather than the
numbers, so tuning any one of them stays possible and tuning it into a dead zone
does not.
"""

from __future__ import annotations

import re

from bmc_sensor_audit.detect import feeder, generator

# The interval the feeder declares to the engine for every observation. Read off
# the call rather than restated as a constant: a number written twice drifts, and
# the whole point of this file is that these three values are related.
FEED_INTERVAL_SECONDS = 60.0


def _window_seconds(window: str) -> float:
    match = re.fullmatch(r"(\d+)([smhd])", window)
    assert match, f"the generated window {window!r} is not a duration this parses"
    value, unit = int(match.group(1)), match.group(2)
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def test_the_feeder_still_declares_the_interval_this_file_assumes():
    """If the feeder stops declaring 60s, the arithmetic below is about nothing.

    Asserted against the source because the value is a keyword argument on a call
    into the engine, not a module constant this could import. A test whose premise
    silently stopped holding is worse than no test.
    """
    source = (feeder.__file__ and open(feeder.__file__, encoding="utf-8").read())
    assert f"interval_seconds={FEED_INTERVAL_SECONDS}" in source, (
        "the feeder no longer declares the interval this file computes with")


def test_the_window_can_hold_more_samples_than_the_floor():
    """The load-bearing test. See the module docstring.

    In-window capacity is `window / interval - 1`: the engine lays the series out
    backwards from now at the declared spacing, so the oldest of N samples sits at
    `N * interval` and falls outside a window of exactly that length.
    """
    capacity = _window_seconds(generator.WINDOW) / FEED_INTERVAL_SECONDS - 1
    assert capacity >= feeder.STUCK_AT_SAMPLE_FLOOR, (
        f"a {generator.WINDOW} window at {FEED_INTERVAL_SECONDS:g}s per sample "
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
    shift, because the engine is told every sample is a minute old. Only the
    capture stamps carry that, so a run whose walks are unstamped must decline to
    state a span rather than printing a plausible one."""
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
