"""Acceptance test 6: pacing delays and escalating backoff are actually applied."""

from __future__ import annotations

import random

from conftest import FakeClock

from india_xbrl.pacing import Pacer


def test_minimum_spacing_enforced(fake_clock: FakeClock) -> None:
    pacer = Pacer(fake_clock, min_interval=6.0, backoff_base=6.0, rng=random.Random(1))
    pacer.wait_before_request()  # first request: no wait
    assert fake_clock.sleeps == []

    pacer.wait_before_request()  # second request immediately after: must wait
    assert fake_clock.sleeps == [6.0]


def test_spacing_is_gap_not_fixed_delay(fake_clock: FakeClock) -> None:
    pacer = Pacer(fake_clock, min_interval=6.0, backoff_base=6.0, rng=random.Random(1))
    pacer.wait_before_request()
    fake_clock.advance(2.5)  # simulate elapsed time between calls
    pacer.wait_before_request()
    assert fake_clock.sleeps == [3.5]  # only the remaining gap


def test_backoff_escalates_per_attempt(fake_clock: FakeClock) -> None:
    pacer = Pacer(
        fake_clock, min_interval=6.0, backoff_base=6.0,
        jitter_fraction=0.0, rng=random.Random(1),
    )
    d0 = pacer.backoff_sleep(0)
    d1 = pacer.backoff_sleep(1)
    d2 = pacer.backoff_sleep(2)
    assert (d0, d1, d2) == (6.0, 12.0, 18.0)
    assert fake_clock.sleeps == [6.0, 12.0, 18.0]


def test_backoff_jitter_present_but_bounded(fake_clock: FakeClock) -> None:
    pacer = Pacer(
        fake_clock, min_interval=6.0, backoff_base=6.0,
        jitter_fraction=0.25, rng=random.Random(7),
    )
    d = pacer.backoff_sleep(0)
    assert 6.0 <= d <= 6.0 * 1.25 + 1e-9


def test_backoff_is_capped(fake_clock: FakeClock) -> None:
    pacer = Pacer(
        fake_clock, min_interval=6.0, backoff_base=100.0,
        max_backoff=120.0, jitter_fraction=0.5, rng=random.Random(3),
    )
    d = pacer.backoff_sleep(50)  # would be 5100s uncapped
    assert d == 120.0


def test_backoff_after_failure_resets_spacing_clock(fake_clock: FakeClock) -> None:
    pacer = Pacer(
        fake_clock, min_interval=6.0, backoff_base=6.0,
        jitter_fraction=0.0, rng=random.Random(1),
    )
    pacer.wait_before_request()
    pacer.backoff_sleep(0)  # post-failure sleep also counts as spacing
    pacer.wait_before_request()  # immediately after backoff: no extra sleep
    # The backoff sleep credits the spacing gap; the subsequent
    # wait_before_request adds no further sleep.
    assert fake_clock.sleeps == [6.0]


def test_invalid_interval_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        Pacer(FakeClock(), min_interval=0.0)
