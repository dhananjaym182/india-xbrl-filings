"""Acceptance tests 1, 4, 5, 7: resume, ixbrl-never-fetched, no_xbrl_link,
offline pipeline."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from pathlib import Path

from conftest import FakeClock, FakeTransport, make_record

from india_xbrl.fetcher import FetchPlan, Fetcher, plan_from_discovery
from india_xbrl.manifest import Manifest
from india_xbrl.models import DiscoveryRecord, FetchStatus, Source
from india_xbrl.store import PayloadStore

URL_A = "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_A.xml"
URL_B = "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_B.xml"
BODY_A = b"<xbrli:xbrl xmlns:xbrli='http://www.xbrl.org/2003/instance'>A</xbrli:xbrl>"
BODY_B = b"<xbrli:xbrl xmlns:xbrli='http://www.xbrl.org/2003/instance'>B</xbrli:xbrl>"


def _plan(
    url: str | None,
    symbol: str = "AAA",
    filing_id: str | None = None,
    consolidated: bool = False,
    period_end: dt.date | None = dt.date(2021, 3, 31),
) -> FetchPlan:
    return FetchPlan(
        symbol=symbol,
        filing_id=filing_id,
        xbrl_url=url,
        period_end=period_end,
        consolidated=consolidated,
        source=Source.NSE_LEGACY,
    )


def _fetcher(
    fake_transport: FakeTransport,
    store: PayloadStore,
    manifest: Manifest,
    fake_clock: FakeClock,
    *,
    workers: int = 4,
    min_interval: float = 6.0,
    max_attempts: int = 4,
    persist: Callable[[], None] | None = None,
) -> Fetcher:
    return Fetcher(
        fake_transport,
        store,
        manifest,
        clock=fake_clock,
        min_interval=min_interval,
        backoff_base=6.0,
        workers=workers,
        max_attempts=max_attempts,
        persist=persist,
    )


# ---------------------------------------------------------------------- #
# Acceptance 7: full pipeline offline, mocked transport, no network
# ---------------------------------------------------------------------- #


def test_offline_pipeline_downloads_and_records(
    fake_transport: FakeTransport, store: PayloadStore, manifest: Manifest,
    fake_clock: FakeClock,
) -> None:
    fake_transport.route(URL_A, 200, BODY_A)
    stats = _fetcher(fake_transport, store, manifest, fake_clock).run([_plan(URL_A)])
    assert stats.downloaded == 1
    rec = manifest.get(("AAA", URL_A))
    assert rec is not None
    assert rec.fetch_status == FetchStatus.OK
    assert rec.raw_sha256
    assert rec.raw_bytes == len(BODY_A)
    assert Path(rec.raw_path or "").is_file()  # byte-exact gzipped payload on disk
    # Provenance recorded.
    assert rec.xbrl_url == URL_A
    assert rec.http_status == 200


def test_offline_pipeline_discovery_to_fetch(
    fake_transport: FakeTransport, store: PayloadStore, manifest: Manifest,
    fake_clock: FakeClock,
) -> None:
    """Full index+fetch path against mocked transport: discovery rows feed
    plans feed downloads, with zero network access."""
    legacy_payload = (
        b'[{"symbol":"ZZZ","company":"ZZZ Ltd","isin":"INE123Z01014",'
        b'"from":"01-Jan-2021","to":"31-Mar-2021","relatingTo":"Quarterly",'
        b'"filingDateTs":"14-May-2021 18:30:00",'
        b'"xbrl":"https://nsearchives.nseindia.com/corporate/xbrl/INDAS_ZZZ.xml",'
        b'"ixbrl":"https://nsearchives.nseindia.com/corporate/ZZZ.html",'
        b'"filingId":"777"}]'
    )
    discovery_url = (
        "https://www.nseindia.com/api/corporates-financial-results"
        "?index=equities&period=Quarterly&from_date=01-01-2021&to_date=31-03-2021"
    )
    xbrl_url = "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_ZZZ.xml"
    fake_transport.route(discovery_url, 200, legacy_payload)
    fake_transport.route(xbrl_url, 200, b"<xbrl/>")

    from india_xbrl.discovery import DiscoveryClient

    client = DiscoveryClient(fake_transport)
    records = client.discover_legacy(dt.date(2021, 1, 1), dt.date(2021, 3, 31))
    assert len(records) == 1
    plans = [plan_from_discovery(r) for r in records]
    stats = _fetcher(fake_transport, store, manifest, fake_clock).run(plans)
    assert stats.downloaded == 1
    assert manifest.get(("ZZZ", "777")) is not None


# ---------------------------------------------------------------------- #
# Acceptance 4: the ixbrl field is never used
# ---------------------------------------------------------------------- #


def test_fetcher_reads_xbrl_field_only(
    fake_transport: FakeTransport, store: PayloadStore, manifest: Manifest,
    fake_clock: FakeClock,
) -> None:
    rec = make_record()
    assert rec.xbrl_url is not None
    rec_ixbrl = DiscoveryRecord(
        symbol=rec.symbol, company=rec.company, isin=rec.isin,
        period_from=rec.period_from, period_to=rec.period_to,
        relating_to=rec.relating_to, filing_ts=rec.filing_ts,
        xbrl_url=rec.xbrl_url,
        ixbrl_url="https://nsearchives.nseindia.com/corporate/rendered_table.html",
        attachment_type=rec.attachment_type, filing_id=rec.filing_id,
        source=rec.source, consolidated=rec.consolidated, audited=rec.audited,
        cumulative=rec.cumulative, ind_as=rec.ind_as,
    )
    plan = plan_from_discovery(rec_ixbrl)
    # The plan carries no ixbrl URL *by construction*.
    assert not any("ixbrl" in f for f in plan.__dataclass_fields__)
    assert plan.xbrl_url == rec_ixbrl.xbrl_url

    assert rec_ixbrl.xbrl_url is not None
    fake_transport.route(rec_ixbrl.xbrl_url, 200, BODY_A)
    _fetcher(fake_transport, store, manifest, fake_clock).run([plan])

    requested = [u for u, _ in fake_transport.requests]
    assert requested == [rec_ixbrl.xbrl_url]
    assert not any("rendered_table.html" in u for u in requested)


# ---------------------------------------------------------------------- #
# Acceptance 5: no_xbrl_link is not an error and never retried
# ---------------------------------------------------------------------- #


def test_no_xbrl_link_recorded_without_retry(
    fake_transport: FakeTransport, store: PayloadStore, manifest: Manifest,
    fake_clock: FakeClock,
) -> None:
    plan = FetchPlan(symbol="NOX", filing_id="1", xbrl_url=None)
    stats = _fetcher(fake_transport, store, manifest, fake_clock).run([plan])
    assert stats.no_xbrl_link == 1
    assert stats.failed == 0
    rec = manifest.get(("NOX", "1"))
    assert rec is not None
    assert rec.fetch_status == FetchStatus.NO_XBRL_LINK
    assert rec.retry_count == 0
    assert fake_transport.requests == []  # nothing requested at all
    assert fake_clock.sleeps == []  # no backoff sleeps either
    # A second run must not retry it either.
    stats2 = _fetcher(fake_transport, store, manifest, fake_clock).run([plan])
    assert stats2.no_xbrl_link == 1
    assert fake_transport.requests == []


def test_http_404_is_recorded_not_raised(
    fake_transport: FakeTransport, store: PayloadStore, manifest: Manifest,
    fake_clock: FakeClock,
) -> None:
    fake_transport.route(URL_A, 404, b"not found")
    stats = _fetcher(
        fake_transport, store, manifest, fake_clock, workers=1
    ).run([_plan(URL_A)])
    rec = manifest.get(("AAA", URL_A))
    assert rec is not None
    assert rec.fetch_status == FetchStatus.FAILED
    assert rec.http_status == 404
    assert stats.failed == 1


# ---------------------------------------------------------------------- #
# Acceptance 1: resume is real
# ---------------------------------------------------------------------- #


def test_resume_skips_verified_ok_records(
    fake_transport: FakeTransport, store: PayloadStore, manifest: Manifest,
    fake_clock: FakeClock, tmp_path: Path,
) -> None:
    fake_transport.route(URL_A, 200, BODY_A)
    manifest_path = tmp_path / "manifest.jsonl"

    def persist() -> None:
        manifest.save(manifest_path)

    # "Interrupted" run: A succeeds, B's transport blows up, max 1 attempt.
    fake_transport.route_exc(URL_B, ConnectionError("killed mid-run"))
    fetcher = _fetcher(
        fake_transport, store, manifest, fake_clock, workers=1,
        max_attempts=1, persist=persist,
    )
    stats = fetcher.run([_plan(URL_A), _plan(URL_B, symbol="BBB")])
    assert stats.downloaded == 1
    assert stats.failed == 1
    manifest.save(manifest_path)

    # Restart with a working transport for B.
    transport2 = FakeTransport()
    transport2.clock = fake_clock
    transport2.route(URL_A, 200, BODY_A)
    transport2.route(URL_B, 200, BODY_B)
    manifest2 = Manifest.load(manifest_path)
    fetcher2 = Fetcher(
        transport2, store, manifest2, clock=fake_clock,
        min_interval=6.0, backoff_base=6.0, workers=1,
    )
    stats2 = fetcher2.run([_plan(URL_A), _plan(URL_B, symbol="BBB")])

    # Zero re-downloads of the ok record; B completes.
    assert [u for u, _ in transport2.requests] == [URL_B]
    assert stats2.skipped_already_ok == 1
    assert stats2.downloaded == 2  # manifest total: A (kept) + B (new)

    # Final manifest equals an uninterrupted run's manifest.
    transport3 = FakeTransport()
    transport3.clock = fake_clock
    transport3.route(URL_A, 200, BODY_A)
    transport3.route(URL_B, 200, BODY_B)
    fresh = Manifest()
    Fetcher(
        transport3, store, fresh, clock=fake_clock,
        min_interval=6.0, backoff_base=6.0, workers=1,
    ).run([_plan(URL_A), _plan(URL_B, symbol="BBB")])
    interrupted = sorted(
        (r.symbol, r.filing_id, r.raw_sha256, r.fetch_status.value)
        for r in manifest2
    )
    uninterrupted = sorted(
        (r.symbol, r.filing_id, r.raw_sha256, r.fetch_status.value) for r in fresh
    )
    assert interrupted == uninterrupted


def test_resume_refetches_when_file_corrupted(
    fake_transport: FakeTransport, store: PayloadStore, manifest: Manifest,
    fake_clock: FakeClock,
) -> None:
    fake_transport.route(URL_A, 200, BODY_A)
    _fetcher(fake_transport, store, manifest, fake_clock).run([_plan(URL_A)])
    rec = manifest.get(("AAA", URL_A))
    assert rec and rec.raw_path
    # Corrupt the stored file.
    Path(rec.raw_path).write_bytes(b"garbage-not-gzip")
    # Self-healing: the record is re-fetched.
    stats = _fetcher(fake_transport, store, manifest, fake_clock).run([_plan(URL_A)])
    assert stats.downloaded == 1
    assert len(fake_transport.requests) == 2


def test_revisions_are_preserved_not_collapsed(
    fake_transport: FakeTransport, store: PayloadStore, manifest: Manifest,
    fake_clock: FakeClock,
) -> None:
    """Same (symbol, period_end), two filing ids: both are kept."""
    u1 = URL_A
    u2 = "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_A_rev2.xml"
    fake_transport.route(u1, 200, BODY_A)
    fake_transport.route(u2, 200, BODY_B)
    plans = [
        _plan(u1, filing_id="101"),
        _plan(u2, filing_id="102"),
    ]
    stats = _fetcher(fake_transport, store, manifest, fake_clock).run(plans)
    assert stats.downloaded == 2
    assert manifest.get(("AAA", "101")) is not None
    assert manifest.get(("AAA", "102")) is not None
    assert len(manifest) == 2


def test_standalone_skipped_but_recorded(
    fake_transport: FakeTransport, store: PayloadStore, manifest: Manifest,
    fake_clock: FakeClock,
) -> None:
    consolidated = _plan(URL_A, filing_id="201", consolidated=True)
    standalone = _plan(URL_B, filing_id="202", consolidated=False)
    fake_transport.route(URL_A, 200, BODY_A)
    fake_transport.route(URL_B, 200, BODY_B)
    stats = _fetcher(fake_transport, store, manifest, fake_clock, workers=1).run(
        [consolidated, standalone], skip_standalone_redundant=True
    )
    rec = manifest.get(("AAA", "202"))
    assert rec is not None
    assert rec.fetch_status == FetchStatus.SKIPPED_STANDALONE_REDUNDANT
    assert stats.skipped_standalone == 1
    # Not deleted: still in the manifest with its own recorded outcome.
    assert [u for u, _ in fake_transport.requests] == [URL_A]


# ---------------------------------------------------------------------- #
# Acceptance 6 (fetcher level): pacing + backoff applied during fetches
# ---------------------------------------------------------------------- #


def test_fetcher_enforces_spacing_between_requests(
    fake_transport: FakeTransport, store: PayloadStore, manifest: Manifest,
    fake_clock: FakeClock,
) -> None:
    u2 = "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_C.xml"
    fake_transport.route(URL_A, 200, BODY_A)
    fake_transport.route(u2, 200, BODY_B)
    _fetcher(
        fake_transport, store, manifest, fake_clock, workers=1, min_interval=6.0
    ).run([_plan(URL_A), _plan(u2, symbol="CCC")])
    assert len(fake_transport.requests) == 2
    # Gap between request starts must be >= min_interval.
    gap = fake_transport.requests[1][1] - fake_transport.requests[0][1]
    assert gap >= 6.0


def test_fetcher_backs_off_between_attempts(
    fake_transport: FakeTransport, store: PayloadStore, manifest: Manifest,
    fake_clock: FakeClock,
) -> None:
    fake_transport.route(URL_A, 500, b"server error")
    fake_transport.route(URL_A, 200, BODY_A)  # second attempt succeeds
    stats = _fetcher(
        fake_transport, store, manifest, fake_clock, workers=1, max_attempts=3
    ).run([_plan(URL_A)])
    assert stats.downloaded == 1
    assert len(fake_transport.requests) == 2
    rec = manifest.get(("AAA", URL_A))
    assert rec is not None and rec.retry_count == 1
    # The retry waited >= backoff_base (6s) before the second attempt.
    gap = fake_transport.requests[1][1] - fake_transport.requests[0][1]
    assert gap >= 6.0
    assert any(s >= 6.0 for s in fake_clock.sleeps)
