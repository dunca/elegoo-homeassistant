"""
A steadier remaining-time estimate.

The Centauri Carbon 2 re-estimates its remaining time from how fast it is
printing right now. On slow first layers the figure climbs (16.1 -> 17.2 min in
18 s on a 16-minute job), then drops faster than the clock once the printer
speeds up. Shown as is, the remaining time and the finish time jump about.

``RemainingEstimate`` follows the printer's figure with two limits: a higher
figure is only taken once it has held for ``RISE_HOLD`` seconds, and the shown
time never falls faster than ``MAX_FALL_RATE`` times the clock. It does not
count down on its own: on 3 Oct the printer's figure sat still for the first
90 s of printing, so a clock countdown would have run ahead and then jumped
back up. While the job is not printing (preparing, paused) the printer's figure
passes through unchanged.
"""

from __future__ import annotations

RISE_HOLD = 60.0
MAX_FALL_RATE = 1.5
# a figure this close to the shown one is the same estimate, not a rise
TOLERANCE = 5.0
# no update for this long (a restart, a lost connection): start again
MAX_GAP = 120.0


class RemainingEstimate:
    """Smooths the printer's remaining-time figure, in seconds."""

    def __init__(self) -> None:
        """Start with no estimate."""
        self._shown: float | None = None
        self._last: float | None = None
        self._rise_since: float | None = None

    def reset(self) -> None:
        """Forget the current estimate."""
        self._shown = None
        self._last = None
        self._rise_since = None

    def update(self, raw: float | None, now: float, *, printing: bool) -> float | None:
        """Take the printer's figure at ``now`` (monotonic); return the one to show."""
        if raw is None or not printing:
            self.reset()
            return raw
        if self._shown is None or self._last is None or now - self._last > MAX_GAP:
            self._shown, self._last, self._rise_since = raw, now, None
            return raw

        elapsed = max(0.0, now - self._last)
        self._last = now
        if raw > self._shown + TOLERANCE:
            if self._rise_since is None:
                self._rise_since = now
            if now - self._rise_since >= RISE_HOLD:
                self._shown, self._rise_since = raw, None
        else:
            self._rise_since = None
            self._shown = max(raw, self._shown - elapsed * MAX_FALL_RATE, 0.0)
        return self._shown
