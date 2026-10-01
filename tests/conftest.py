"""Shared fixtures: fake transport + clock, tmp store/manifest. No network."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from india_xbrl.models import DiscoveryRecord

from india_xbrl.manifest import Manifest
from india_xbrl.pacing import Pacer
from india_xbrl.store import PayloadStore
from india_xbrl.transport import Clock, Response


class FakeTransport:
    """Scripted transport: maps URL -> queued (status, body) or exception.

    Multiple routes for the same URL are served in order (useful for
    "first request 500, second request 200"); the last one repeats.
    Records every request (url, monotonic-time) for pacing assertions.
    """

    def __init__(self) -> None:
        self.routes: dict[str, list[Response | Exception]] = {}
        self.requests: list[tuple[str, float]] = []

    def route(self, url: str, status: int, body: bytes) -> None:
        self.routes.setdefault(url, []).append(
            Response(status_code=status, content=body, url=url)
        )

    def route_exc(self, url: str, exc: Exception) -> None:
        self.routes.setdefault(url, []).append(exc)

    def get(self, url: str, *, timeout: float = 30.0) -> Response:
        self.requests.append((url, self.clock.monotonic()))
        queue = self.routes.get(url)
        if not queue:
            raise AssertionError(f"unexpected request: {url}")
        entry = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(entry, Exception):
            raise entry
        return entry

    clock: Clock


class FakeClock:
    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self._now = 0.0

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += seconds

    def advance(self, seconds: float) -> None:
        """Move time forward without recording a sleep (test-driven elapsed time)."""
        self._now += seconds

    def monotonic(self) -> float:
        return self._now


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_transport(fake_clock: FakeClock) -> FakeTransport:
    t = FakeTransport()
    t.clock = fake_clock
    return t


@pytest.fixture
def store(tmp_path: Path) -> PayloadStore:
    return PayloadStore(tmp_path / "payloads")


@pytest.fixture
def manifest() -> Manifest:
    return Manifest()


@pytest.fixture
def pacer(fake_clock: FakeClock) -> Pacer:
    import random

    return Pacer(
        fake_clock,
        min_interval=6.0,
        backoff_base=6.0,
        rng=random.Random(42),  # deterministic jitter
    )


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_bse_fin() -> bytes:
    return (FIXTURES / "in-bse-fin" / "sample_legacy_2021.xml").read_bytes()


@pytest.fixture
def fixture_capmkt() -> bytes:
    return (FIXTURES / "in-capmkt" / "sample_integrated_2025.xml").read_bytes()


def make_record(
    symbol: str = "TCS",
    filing_id: str | None = "102938",
    xbrl_url: str | None = "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_TCS_2021.xml",
    period_end: dt.date | None = dt.date(2021, 3, 31),
    consolidated: bool = False,
    source: str = "nse_legacy_financial_results",
) -> DiscoveryRecord:
    from india_xbrl.models import DiscoveryRecord, Source

    return DiscoveryRecord(
        symbol=symbol,
        company=f"{symbol} Ltd",
        isin="INE002A08JTW",
        period_from=dt.date(2021, 1, 1) if period_end else None,
        period_to=period_end,
        relating_to="Quarterly",
        filing_ts=dt.datetime(2021, 5, 14, 18, 30, 0),
        xbrl_url=xbrl_url,
        ixbrl_url=None,
        attachment_type="XBRL",
        filing_id=filing_id,
        source=Source(source),
        consolidated=consolidated,
        audited=True,
        cumulative=False,
        ind_as=True,
    )
