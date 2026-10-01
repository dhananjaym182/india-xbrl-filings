"""Discovery payload parsing, pagination, and date-window logic (offline)."""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import FakeTransport

from india_xbrl.discovery import (
    DiscoveryClient,
    DiscoveryError,
    _clean_url,
    _date_windows,
    _parse_integrated_payload,
    _parse_legacy_payload,
)
from india_xbrl.models import Source


LEGACY_ROW = (
    b'[{"symbol":"reliance","company":"Reliance Industries Limited",'
    b'"isin":"INE002A01018","from":"01-Jan-2019","to":"31-Mar-2019",'
    b'"relatingTo":"Quarterly","filingDateTs":"14-May-2019 18:30:00",'
    b'"xbrl":"https://nsearchives.nseindia.com/corporate/xbrl/INDAS_REL_2019.xml",'
    b'"ixbrl":"https://nsearchives.nseindia.com/corporate/REL_2019.html",'
    b'"filingId":"4054421","attach":"XBRL"}]'
)

INTEGRATED_PAGE = (
    b'{"data":[{"symbol":"TCS","company":"Tata Consultancy Services Ltd",'
    b'"from":"01-Oct-2025","to":"31-Dec-2025","relatingTo":"Quarterly",'
    b'"filingDateTs":"12-Jan-2026 17:45:00",'
    b'"xbrl":"https://nsearchives.nseindia.com/corporate/xbrl/INDAS_TCS_Q3.xml",'
    b'"ixbrl":"https://nsearchives.nseindia.com/corporate/TCS_Q3.html",'
    b'"filingId":"90211"}],'
    b'"size":1,"page":1,"totalCount":1}'
)


def test_parse_legacy_row() -> None:
    records = _parse_legacy_payload(LEGACY_ROW, (dt.date(2019, 1, 1), dt.date(2019, 3, 31)))
    assert len(records) == 1
    r = records[0]
    assert r.symbol == "RELIANCE"  # normalized upper
    assert r.source == Source.NSE_LEGACY
    assert r.period_from == dt.date(2019, 1, 1)
    assert r.period_to == dt.date(2019, 3, 31)
    assert r.filing_ts == dt.datetime(2019, 5, 14, 18, 30)
    assert r.xbrl_url and r.xbrl_url.startswith("https://nsearchives")
    assert r.ixbrl_url and r.ixbrl_url.endswith(".html")


def test_parse_integrated_page() -> None:
    records, total, done = _parse_integrated_payload(
        INTEGRATED_PAGE, (dt.date(2025, 10, 1), dt.date(2025, 12, 31)), page=1
    )
    assert len(records) == 1
    assert records[0].source == Source.NSE_INTEGRATED
    assert total == 1
    # A non-empty page is not a stop signal by itself; the caller compares
    # the cumulative count against totalCount.
    assert done is False


def test_parse_integrated_empty_page_is_done() -> None:
    records, total, done = _parse_integrated_payload(
        b'{"data":[],"size":50,"page":3,"totalCount":101}',
        (dt.date(2025, 10, 1), dt.date(2025, 12, 31)),
        page=3,
    )
    assert records == []
    assert done is True


LIVE_INTEGRATED_ROW = (
    # Field names and value shapes exactly as measured live 2026-10-01 on
    # integrated-filing-results: seq_Id (no filingId), qe_Date, creation_Date,
    # word classifications, and the "-" xbrl sentinel pre-joined into a URL.
    b'{"data":[{"symbol":"TCS","smName":"Tata Consultancy Services Ltd",'
    b'"seq_Id":"199238","qe_Date":"31-MAR-2026",'
    b'"creation_Date":"30-Sep-2026 18:56:46","broadCastDate":null,'
    b'"audited":"Un-Audited","consolidated":"Consolidated",'
    b'"indAs":null,"type_Sub":"Original",'
    b'"xbrl":"https://nsearchives.nseindia.com/corporate/xbrl/-",'
    b'"ixbrl":"https://nsearchives.nseindia.com/corporate/ixbrl/TCS.html"}],'
    b'"size":1,"page":1,"totalCount":1}'
)


def test_parse_live_integrated_row_shape() -> None:
    records, _, _ = _parse_integrated_payload(
        LIVE_INTEGRATED_ROW, (dt.date(2026, 9, 1), dt.date(2026, 9, 30)), page=1
    )
    r = records[0]
    assert r.filing_id == "199238"  # seq_Id is the row identity
    assert r.period_to == dt.date(2026, 3, 31)  # qe_Date, UPPERCASE month
    assert r.filing_ts == dt.datetime(2026, 9, 30, 18, 56, 46)  # creation_Date
    assert r.audited is False  # "Un-Audited"
    assert r.consolidated is True  # "Consolidated"
    assert r.ind_as is False  # null
    assert r.xbrl_url is None  # ".../xbrl/-" sentinel
    assert r.ixbrl_url is not None


LIVE_LEGACY_ROW = (
    # Live corporates-financial-results shape: seqNumber, toDate/fromDate,
    # word classifications.
    b'[{"symbol":"reliance","companyName":"Reliance Industries Limited",'
    b'"seqNumber":"1197587","fromDate":"01-Oct-2024","toDate":"31-Dec-2024",'
    b'"broadCastDate":"20-Feb-2026 17:01:35","filingDate":"20-Feb-2026 17:01",'
    b'"audited":"Audited","consolidated":"Non-Consolidated",'
    b'"cumulative":"Non-cumulative","indAs":"Ind-AS New",'
    b'"xbrl":"https://nsearchives.nseindia.com/corporate/xbrl/REL.xml",'
    b'"ixbrl":"-"}]'
)


def test_parse_live_legacy_row_shape() -> None:
    records = _parse_legacy_payload(
        LIVE_LEGACY_ROW, (dt.date(2026, 1, 1), dt.date(2026, 3, 31))
    )
    r = records[0]
    assert r.filing_id == "1197587"  # seqNumber is the row identity
    assert r.period_to == dt.date(2024, 12, 31)  # toDate
    assert r.period_from == dt.date(2024, 10, 1)  # fromDate
    assert r.filing_ts == dt.datetime(2026, 2, 20, 17, 1, 35)  # broadCastDate
    assert r.audited is True
    assert r.consolidated is False  # "Non-Consolidated"
    assert r.cumulative is False  # "Non-cumulative"
    assert r.ind_as is True  # "Ind-AS New"
    assert r.ixbrl_url is None  # "-" sentinel


def test_date_windows_chunk_100_days() -> None:
    windows = list(_date_windows(dt.date(2019, 1, 1), dt.date(2019, 12, 31)))
    assert all((end - start).days < 100 for start, end in windows)
    # Windows tile the range without gaps or overlap.
    assert windows[0][0] == dt.date(2019, 1, 1)
    assert windows[-1][1] == dt.date(2019, 12, 31)
    for (_, end1), (start2, _) in zip(windows, windows[1:]):
        assert start2 == end1 + dt.timedelta(days=1)


def test_legacy_discovery_paginates_windows(
    fake_transport: FakeTransport, fake_clock: object
) -> None:
    # Two windows -> two URLs, both served (100-day windowing: Jan 1 + 99d
    # ends Apr 10, so window 2 starts Apr 11).
    url1 = (
        "https://www.nseindia.com/api/corporates-financial-results"
        "?index=equities&period=Quarterly&from_date=01-01-2019&to_date=10-04-2019"
    )
    url2 = (
        "https://www.nseindia.com/api/corporates-financial-results"
        "?index=equities&period=Quarterly&from_date=11-04-2019&to_date=30-06-2019"
    )
    fake_transport.route(url1, 200, LEGACY_ROW)
    fake_transport.route(url2, 200, LEGACY_ROW)
    client = DiscoveryClient(fake_transport)
    records = client.discover_legacy(dt.date(2019, 1, 1), dt.date(2019, 6, 30))
    assert len(records) == 2


def test_integrated_discovery_pages_until_total(
    fake_transport: FakeTransport, fake_clock: object
) -> None:
    base = (
        "https://www.nseindia.com/api/integrated-filing-results"
        "?index=equities&from_date=01-10-2025&to_date=31-12-2025&page={page}"
    )
    page1 = (
        b'{"data":[{"symbol":"AAA","company":"A Ltd","filingId":"1",'
        b'"xbrl":"https://nsearchives.nseindia.com/corporate/xbrl/A.xml"}],'
        b'"size":2,"page":1,"totalCount":2}'
    )
    page2 = (
        b'{"data":[{"symbol":"BBB","company":"B Ltd","filingId":"2",'
        b'"xbrl":"https://nsearchives.nseindia.com/corporate/xbrl/B.xml"}],'
        b'"size":2,"page":2,"totalCount":2}'
    )
    fake_transport.route(base.format(page=1), 200, page1)
    fake_transport.route(base.format(page=2), 200, page2)
    client = DiscoveryClient(fake_transport)
    records = client.discover_integrated(dt.date(2025, 10, 1), dt.date(2025, 12, 31))
    assert len(records) == 2
    assert {r.symbol for r in records} == {"AAA", "BBB"}


def test_integrated_starts_2025(fake_transport: FakeTransport, fake_clock: object) -> None:
    client = DiscoveryClient(fake_transport)
    assert client.discover_integrated(dt.date(2019, 1, 1), dt.date(2020, 12, 31)) == []
    assert fake_transport.requests == []


def test_http_error_raises_discovery_error(
    fake_transport: FakeTransport, fake_clock: object
) -> None:
    url = (
        "https://www.nseindia.com/api/corporates-financial-results"
        "?index=equities&period=Quarterly&from_date=01-01-2019&to_date=31-03-2019"
    )
    fake_transport.route(url, 503, b"busy")
    client = DiscoveryClient(fake_transport)
    with pytest.raises(DiscoveryError):
        client.discover_legacy(dt.date(2019, 1, 1), dt.date(2019, 3, 31))


def test_missing_xbrl_field_yields_none() -> None:
    payload = (
        b'[{"symbol":"SMALL","company":"Small Co","filingId":"9",'
        b'"ixbrl":"https://nsearchives.nseindia.com/corporate/SMALL.html"}]'
    )
    records = _parse_legacy_payload(payload, (dt.date(2021, 1, 1), dt.date(2021, 3, 31)))
    assert records[0].xbrl_url is None
    assert records[0].ixbrl_url is not None


def test_clean_url_filters_dangling_sentinel() -> None:
    """NSE's null sentinel leaks through as '-' -- bare or pre-joined into
    an absolute URL (.../xbrl/-, which 404s live). Both mean: no attachment."""
    assert _clean_url("-") is None
    assert _clean_url("https://nsearchives.nseindia.com/corporate/xbrl/-") is None
    assert _clean_url("/corporate/xbrl/-") is None
    assert _clean_url("na") is None
    assert _clean_url("") is None
    assert _clean_url(None) is None
    assert _clean_url(
        "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_TCS_2021.xml"
    ) == "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_TCS_2021.xml"
    assert _clean_url("ATTACH.xml") == "ATTACH.xml"  # relative names pass through
