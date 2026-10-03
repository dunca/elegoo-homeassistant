"""Tests for the steadier remaining-time estimate."""

from __future__ import annotations

from itertools import pairwise

from custom_components.elegoo_printer.remaining_estimate import (
    TOLERANCE,
    RemainingEstimate,
)

# The printer's remaining time (minutes) on 3 Oct 2026, every 2 s from
# 13:58:38, 90 s into printing. It sat at 16.13 since printing began, climbed
# on the slow first layers, then fell about twice as fast as the clock.
OCT_3 = [
    16.25, 16.3667, 16.5667, 16.7333, 16.8667, 17.0, 17.1, 17.1667, 17.2,
    17.2167, 17.2167, 17.2, 17.1667, 17.1333, 17.0667, 17.0, 16.9333, 16.8667,
    16.8, 16.7333, 16.6667, 16.6, 16.5333, 16.45, 16.35, 16.2833, 16.2167,
    16.15, 16.0833, 16.0167, 15.9833, 15.95, 15.9167, 15.8833, 15.85, 15.8167,
    15.7833, 15.75, 15.7167, 15.6833, 15.65, 15.6167, 15.5833, 15.55, 15.5167,
    15.4833, 15.45, 15.4167, 15.3833, 15.35, 15.3,
]  # fmt: skip


def _replay(estimate: RemainingEstimate) -> list[float]:
    estimate.update(16.1333 * 60, 0.0, printing=True)
    shown = []
    for i, minutes in enumerate(OCT_3):
        shown.append(estimate.update(minutes * 60, 90.0 + 2 * i, printing=True))
    return shown


def test_the_first_layer_climb_never_shows() -> None:
    shown = _replay(RemainingEstimate())
    start = 16.1333 * 60
    assert max(shown) <= start + TOLERANCE
    assert all(b <= a + TOLERANCE for a, b in pairwise(shown))


def test_it_catches_up_with_the_printer_once_it_settles() -> None:
    shown = _replay(RemainingEstimate())
    assert abs(shown[-1] - OCT_3[-1] * 60) < 10


def test_it_never_falls_faster_than_one_and_a_half_times_the_clock() -> None:
    estimate = RemainingEstimate()
    estimate.update(1200.0, 0.0, printing=True)
    assert estimate.update(600.0, 10.0, printing=True) == 1185.0


def test_a_lasting_rise_is_taken_after_a_minute() -> None:
    estimate = RemainingEstimate()
    estimate.update(600.0, 0.0, printing=True)
    assert estimate.update(900.0, 30.0, printing=True) == 600.0
    assert estimate.update(900.0, 59.0, printing=True) == 600.0
    assert estimate.update(900.0, 91.0, printing=True) == 900.0


def test_a_short_rise_is_ignored() -> None:
    estimate = RemainingEstimate()
    estimate.update(600.0, 0.0, printing=True)
    estimate.update(700.0, 20.0, printing=True)
    assert estimate.update(598.0, 40.0, printing=True) == 598.0
    # the rise timer started again, so a new rise waits a full minute
    assert estimate.update(700.0, 70.0, printing=True) == 598.0


def test_preparing_or_paused_passes_the_printer_figure_through() -> None:
    estimate = RemainingEstimate()
    estimate.update(600.0, 0.0, printing=True)
    assert estimate.update(900.0, 10.0, printing=False) == 900.0
    # printing again starts from the printer's figure
    assert estimate.update(950.0, 20.0, printing=True) == 950.0


def test_a_long_gap_starts_again() -> None:
    estimate = RemainingEstimate()
    estimate.update(600.0, 0.0, printing=True)
    assert estimate.update(900.0, 500.0, printing=True) == 900.0


def test_unknown_passes_through() -> None:
    estimate = RemainingEstimate()
    assert estimate.update(None, 0.0, printing=True) is None
