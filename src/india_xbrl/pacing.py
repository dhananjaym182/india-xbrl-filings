"""Politeness layer: pacing and escalating backoff with jitter.

Measured against nsearchives.nseindia.com (2026-10-01):

- 0.6 s spacing -> 8 of 20 fetches FAILED.
- 6 s spacing with per-attempt escalating backoff -> every retry succeeded.
- Throughput is bounded by NSE holding connections ~30 s, so it scales with
  concurrency (default 12 workers), not with a shorter delay.

The delay is applied per attempt: ``backoff = base * (attempt + 1)`` plus
random jitter. Without the jitter, concurrent workers retry in lockstep and
re-trigger the rate limit together -- this is the bug class that gets an IP
banned, so it is enforced here and asserted in tests.
"""

from __future__ import annotations

import random

from india_xbrl.transport import (
    DEFAULT_BACKOFF_BASE,
    DEFAULT_MIN_INTERVAL,
    JITTER_FRACTION,
    MAX_BACKOFF_SECONDS,
    Clock,
)


class Pacer:
    """Enforces minimum spacing between requests and computes backoff sleeps."""

    def __init__(
        self,
        clock: Clock,
        *,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        jitter_fraction: float = JITTER_FRACTION,
        max_backoff: float = MAX_BACKOFF_SECONDS,
        rng: random.Random | None = None,
    ) -> None:
        if min_interval <= 0:
            raise ValueError("min_interval must be positive")
        self._clock = clock
        self._min_interval = min_interval
        self._backoff_base = backoff_base
        self._jitter_fraction = jitter_fraction
        self._max_backoff = max_backoff
        self._rng = rng or random.Random()
        self._last_request_at: float | None = None

    def wait_before_request(self) -> None:
        """Sleep so that consecutive requests are at least min_interval apart."""
        now = self._clock.monotonic()
        if self._last_request_at is None:
            self._last_request_at = now
            return
        elapsed = now - self._last_request_at
        remaining = self._min_interval - elapsed
        if remaining > 0:
            self._clock.sleep(remaining)
        self._last_request_at = self._clock.monotonic()

    def backoff_sleep(self, attempt: int) -> float:
        """Sleep for ``base * (attempt + 1)`` plus jitter; returns the duration.

        ``attempt`` is zero-based: the first retry sleeps ~base seconds, the
        second ~2*base, and so on.
        """
        attempt = max(0, attempt)
        target = self._backoff_base * (attempt + 1)
        jitter = self._rng.uniform(0.0, target * self._jitter_fraction)
        duration = min(target + jitter, self._max_backoff)
        self._clock.sleep(duration)
        # The cool-down just served as the spacing gap too: credit it so the
        # next request is not delayed twice.
        self._last_request_at = self._clock.monotonic() - self._min_interval
        return duration

