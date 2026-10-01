"""BSE discovery: windows, pagination, period parsing, RSS instance mapping."""

from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path

from conftest import FakeClock, FakeTransport

from india_xbrl.bse import (
    BSE_ANN_SUBCATEGORY,
    BSE_FIN_RESULTS_RSS,
    BseDiscoveryClient,
    _parse_ann_subcategory,
    _period_end_from_subject,
)
from india_xbrl.manifest import Manifest
from india_xbrl.models import FetchStatus, Source
from india_xbrl.store import PayloadStore

ANN_ROW = {
    "NEWSID": "51b9e923-1446-45eb-b404-0f0ac7f61a00",
    "SCRIP_CD": 530747,
    "XML_NAME": "ANN_530747_51B9E923-1446-45EB-B404-0F0AC7F61A00",
    "NEWSSUB": (
        "Challani Capital Ltd - 530747 - Un-Audited Financial Results & "
        "Limited Review Report For The Quarter Ended 31/12/2018.<BR>"
    ),
    "NEWS_DT": "2019-01-07T16:47:05.153",
    "CATEGORYNAME": "Result",
    "SUBCATNAME": "Financial Results",
    "ATTACHMENTNAME": "abc.pdf",
    "SLONGNAME": "Challani Capital Ltd",
}
ANN_PAGE_ONE = json.dumps(
    {"Table": [ANN_ROW] * 3, "Table1": [{"ROWCNT": 3}]}
).encode()
ANN_PAGE_EMPTY = json.dumps({"Table": [], "Table1": [{"ROWCNT": 3}]}).encode()
ANN_PAGE_51 = json.dumps(
    {"Table": [dict(ANN_ROW, NEWSID=f"id-{i}") for i in range(51)],
     "Table1": [{"ROWCNT": 52}]}
).encode()

RSS_SAMPLE = (
    '<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
    "<title>BSE Latest FINANCIAL RESULTS</title><item>"
    "<title>Impex Ferro Tech Ltd (532614)</title>"
    "<link>https://www.bseindia.com/XBRLFILES/IFIndasDuplicateUploadDocument/"
    "Integrated_Finance_Ind_As_532614_110202617208_IFIndAs.html</link>"
    "<description>Unaudited | Standalone|PERIOD START DATE : 01-01-2026 | "
    "PERIOD END DATE : 31-03-2026|IND AS/NON IND AS : IND_AS</description>"
    "</item><item>"
    "<title>Rungta Irrigation Ltd (530449)</title>"
    "<link>https://www.bseindia.com/XBRLFILES/IFIndasDuplicateUploadDocument/"
    "Integrated_Finance_Ind_As_530449_110202617335_IFIndAs.xml</link>"
    "<description>Audited | Consolidated|PERIOD START DATE : 01-04-2025 | "
    "PERIOD END DATE : 30-09-2025|IND AS/NON IND AS : IND_AS</description>"
    "</item></channel></rss>"
).encode()


def test_period_end_from_subject_variants() -> None:
    assert _period_end_from_subject(
        "Challani Capital Ltd - 530747 - Un-Audited Financial Results & "
        "Limited Review Report For The Quarter Ended 31/12/2018."
    ) == dt.date(2018, 12, 31)
    assert _period_end_from_subject(
        "BF Utilities Ltd - 532430 - Results- Audited Consolidated Financial "
        "Results For Period Ended March 31, 2026"
    ) == dt.date(2026, 3, 31)
    # Live-measured BSE subject typos: well-formed but implausible -> None.
    assert _period_end_from_subject(
        "TTK Prestige Ltd - 517506 - Unaudited Standalone And Consolidated "
        "Financial Results For The Quarter Ended June 30, 3036"
    ) is None
    assert _period_end_from_subject(
        "Goenka Diamond & Jewels Ltd - 533189 - Results-Financial Results "
        "Dec 31, 20925"
    ) is None
    assert _period_end_from_subject("no date here at all") is None
    assert _period_end_from_subject(None) is None


def test_parse_ann_subcategory_rowcount() -> None:
    rows, total = _parse_ann_subcategory(ANN_PAGE_ONE)
    assert len(rows) == 3
    assert total == 3
    rows, total = _parse_ann_subcategory(ANN_PAGE_EMPTY)
    assert rows == []
    assert total == 3  # total is authoritative even when page is empty


def test_discover_results_single_window(
    fake_transport: FakeTransport, fake_clock: FakeClock
) -> None:
    url1 = (
        f"{BSE_ANN_SUBCATEGORY}?pageno=1&strCat=Result"
        "&strPrevDate=20190101&strScrip=&strSearch=P"
        "&strToDate=20190107&strType=C&subcategory=-1"
    )
    fake_transport.route(url1, 200, ANN_PAGE_ONE)
    client = BseDiscoveryClient(fake_transport)
    records = client.discover_results(dt.date(2019, 1, 1), dt.date(2019, 1, 7))
    assert len(records) == 3
    rec = records[0]
    assert rec.source == Source.BSE_ANNOUNCEMENT
    assert rec.symbol == "BSE-530747"
    assert rec.filing_id == ANN_ROW["NEWSID"]
    assert rec.period_to == dt.date(2018, 12, 31)
    assert rec.audited is False  # "Un-Audited"
    assert rec.xbrl_url and rec.xbrl_url.endswith("/abc.pdf")
    assert [u for u, _ in fake_transport.requests] == [url1]


def test_discover_results_paginates_until_rowcount(
    fake_transport: FakeTransport, fake_clock: FakeClock
) -> None:
    url1 = (
        f"{BSE_ANN_SUBCATEGORY}?pageno=1&strCat=Result"
        "&strPrevDate=20190101&strScrip=&strSearch=P"
        "&strToDate=20190107&strType=C&subcategory=-1"
    )
    url2 = url1.replace("pageno=1", "pageno=2")
    fake_transport.route(url1, 200, ANN_PAGE_51)  # 51 rows, total 52
    fake_transport.route(url2, 200, ANN_PAGE_ONE)  # 3 rows -> cumulative 54 >= 52
    client = BseDiscoveryClient(fake_transport)
    records = client.discover_results(dt.date(2019, 1, 1), dt.date(2019, 1, 7))
    assert len(records) == 54
    assert [u for u, _ in fake_transport.requests] == [url1, url2]


def test_discover_windows_across_days(
    fake_transport: FakeTransport, fake_clock: FakeClock
) -> None:
    # A 20-day range with 7-day windows -> 3 windows. The last window is
    # clipped at the range end (20th), never runs past it.
    for d, e in (("20190101", "20190107"), ("20190108", "20190114"),
                 ("20190115", "20190120")):
        url = (
            f"{BSE_ANN_SUBCATEGORY}?pageno=1&strCat=Result"
            f"&strPrevDate={d}&strScrip=&strSearch=P"
            f"&strToDate={e}"
            "&strType=C&subcategory=-1"
        )
        fake_transport.route(url, 200, ANN_PAGE_EMPTY)
    client = BseDiscoveryClient(fake_transport)
    records = client.discover_results(dt.date(2019, 1, 1), dt.date(2019, 1, 20))
    assert records == []
    assert len(fake_transport.requests) == 3


def test_http_error_is_loud_not_recorded(
    fake_transport: FakeTransport, fake_clock: FakeClock
) -> None:
    import pytest

    from india_xbrl.bse import BseDiscoveryError

    url = (
        f"{BSE_ANN_SUBCATEGORY}?pageno=1&strCat=Result"
        "&strPrevDate=20190101&strScrip=&strSearch=P"
        "&strToDate=20190107&strType=C&subcategory=-1"
    )
    fake_transport.route(url, 403, b"Access Denied")
    client = BseDiscoveryClient(fake_transport)
    with pytest.raises(BseDiscoveryError):
        client.discover_results(dt.date(2019, 1, 1), dt.date(2019, 1, 7))


def test_rss_maps_html_link_to_xml_sibling(
    fake_transport: FakeTransport, fake_clock: FakeClock
) -> None:
    fake_transport.route(BSE_FIN_RESULTS_RSS, 200, RSS_SAMPLE)
    client = BseDiscoveryClient(fake_transport)
    records = client.discover_integrated()
    assert len(records) == 2
    first = records[0]
    # The .html link was remapped to its .xml sibling (the plain instance).
    assert first.xbrl_url is not None
    assert first.xbrl_url.endswith(".xml")
    assert first.ixbrl_url is not None
    assert first.ixbrl_url.endswith(".html")
    assert first.symbol == "BSE-532614"
    assert first.period_from == dt.date(2026, 1, 1)
    assert first.period_to == dt.date(2026, 3, 31)
    assert first.ind_as is True
    assert first.audited is False  # "Unaudited"
    # A row whose link is already .xml is kept as-is.
    second = records[1]
    assert second.xbrl_url is not None and second.xbrl_url.endswith(".xml")
    assert second.ixbrl_url is None
    assert second.consolidated is True


def test_bse_rows_fetch_and_record_offline(
    fake_transport: FakeTransport, store: PayloadStore, manifest: Manifest,
    fake_clock: FakeClock,
) -> None:
    """End-to-end: a BSE Result row with a PDF attachment is fetched and
    stored with provenance; a row without attachment becomes no_xbrl_link."""
    from india_xbrl.fetcher import Fetcher, plan_from_discovery

    url1 = (
        f"{BSE_ANN_SUBCATEGORY}?pageno=1&strCat=Result"
        "&strPrevDate=20190101&strScrip=&strSearch=P"
        "&strToDate=20190107&strType=C&subcategory=-1"
    )
    pdf_url = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/abc.pdf"
    fake_transport.route(url1, 200, ANN_PAGE_ONE)
    fake_transport.route(pdf_url, 200, b"%PDF-1.6 fake result pdf")

    client = BseDiscoveryClient(fake_transport)
    records = client.discover_results(dt.date(2019, 1, 1), dt.date(2019, 1, 7))

    # One row without attachment -> no_xbrl_link, never an error.
    import dataclasses

    no_attach = dataclasses.replace(records[0], xbrl_url=None, filing_id="no-link-1")
    plans = [plan_from_discovery(r) for r in [records[0], no_attach]]
    stats = Fetcher(fake_transport, store, manifest, clock=fake_clock, workers=1).run(plans)

    assert stats.downloaded == 1
    assert stats.no_xbrl_link == 1
    rec = manifest.get(("BSE-530747", str(ANN_ROW["NEWSID"])))
    assert rec is not None
    assert rec.fetch_status == FetchStatus.OK
    assert rec.source == Source.BSE_ANNOUNCEMENT
    assert rec.raw_path and Path(rec.raw_path).is_file()
    assert store.read_raw(rec.raw_path) == b"%PDF-1.6 fake result pdf"


def test_bse_payload_gzip_roundtrip(store: PayloadStore) -> None:
    """A fetched PDF must be stored byte-exact (gzipped on disk)."""
    body = b"%PDF-1.6 " + bytes(range(256)) * 3
    path, digest, nbytes = store.write(body, "BSE-530747", "x", "https://bse/a.pdf")
    assert nbytes == len(body)
    assert store.read_raw(path) == body
    with gzip.open(path, "rb") as gz:  # it really is gzip on disk
        assert gz.read() == body
