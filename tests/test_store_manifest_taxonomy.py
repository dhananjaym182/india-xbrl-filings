"""Store integrity, manifest checkpoint semantics, taxonomy package loading."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from india_xbrl.jsonio import discovery_from_json, discovery_to_json, filing_from_json, filing_to_json
from india_xbrl.manifest import DiscoveryIndex, Manifest
from india_xbrl.models import (
    DiscoveryRecord,
    FilingRecord,
    FetchStatus,
    Source,
    TaxonomyFamily,
    parse_nse_date,
    parse_nse_ts,
)
from india_xbrl.store import PayloadStore

URL = "https://nsearchives.nseindia.com/corporate/xbrl/X.xml"


def _record(**over: object) -> FilingRecord:
    base = dict(
        symbol="TCS",
        period_end=dt.date(2021, 3, 31),
        period_from=dt.date(2021, 1, 1),
        filing_id="42",
        isin="INE467B01029",
        relating_to="Quarterly",
        filing_ts=dt.datetime(2021, 5, 14, 18, 30),
        xbrl_url=URL,
        source=Source.NSE_LEGACY,
        consolidated=False,
        audited=True,
        cumulative=False,
        ind_as=True,
        taxonomy_family=TaxonomyFamily.BSE_FIN,
        fetch_status=FetchStatus.OK,
        http_status=200,
        retry_count=1,
        raw_path="/data/payloads/TCS/TCS_42.xml.gz",
        raw_sha256="deadbeef",
        raw_bytes=40960,
        fetched_at=dt.datetime(2021, 5, 14, 19, 0, 5),
    )
    base.update(over)
    return FilingRecord(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------- #
# Manifest: the checkpoint
# ---------------------------------------------------------------------- #


def test_manifest_roundtrip(tmp_path: Path) -> None:
    m = Manifest()
    m.upsert(_record())
    path = tmp_path / "manifest.jsonl"
    m.save(path)
    m2 = Manifest.load(path)
    assert len(m2) == 1
    got = m2.get(("TCS", "42"))
    assert got is not None
    assert got.raw_sha256 == "deadbeef"
    assert got.fetch_status == FetchStatus.OK
    assert got.taxonomy_family == TaxonomyFamily.BSE_FIN
    assert got.period_end == dt.date(2021, 3, 31)
    assert got.filing_ts == dt.datetime(2021, 5, 14, 18, 30)


def test_manifest_save_is_atomic_and_leaves_no_tmp(tmp_path: Path) -> None:
    m = Manifest()
    m.upsert(_record())
    path = tmp_path / "manifest.jsonl"
    m.save(path)
    m.upsert(_record(raw_sha256="cafebabe"))
    m.save(path)
    leftovers = [p for p in tmp_path.iterdir() if p.name != "manifest.jsonl"]
    assert leftovers == []
    lines = path.read_text().splitlines()
    assert len(lines) == 1  # rewritten, not appended twice
    doc = json.loads(lines[0])
    assert doc["raw_sha256"] == "cafebabe"


def test_manifest_revisions_kept_as_distinct_keys() -> None:
    m = Manifest()
    m.upsert(_record(filing_id="1"))
    m.upsert(_record(filing_id="2"))
    assert len(m) == 2
    assert m.get(("TCS", "1")) is not None
    assert m.get(("TCS", "2")) is not None


def test_manifest_line_is_jsonl_inspectable(tmp_path: Path) -> None:
    m = Manifest()
    m.upsert(_record())
    path = tmp_path / "manifest.jsonl"
    m.save(path)
    doc = json.loads(path.read_text().splitlines()[0])
    assert doc["symbol"] == "TCS"
    assert doc["fetch_status"] == "ok"
    assert doc["xbrl_url"] == URL


def test_discovery_index_roundtrip(tmp_path: Path) -> None:
    rec = DiscoveryRecord(
        symbol="INFY", company="Infosys Ltd", isin="INE009A01021",
        period_from=dt.date(2019, 1, 1), period_to=dt.date(2019, 3, 31),
        relating_to="Quarterly", filing_ts=dt.datetime(2019, 4, 16, 17, 0),
        xbrl_url=URL, ixbrl_url="https://x/y.html", attachment_type="XBRL",
        filing_id="77", source=Source.NSE_LEGACY,
        consolidated=True, audited=True, cumulative=False, ind_as=True,
    )
    idx = DiscoveryIndex([rec])
    path = tmp_path / "index.jsonl"
    idx.save(path)
    idx2 = DiscoveryIndex.load(path)
    assert len(idx2.records) == 1
    r2 = idx2.records[0]
    assert r2.symbol == "INFY"
    assert r2.period_to == dt.date(2019, 3, 31)
    assert r2.source == Source.NSE_LEGACY
    assert r2.ixbrl_url == "https://x/y.html"


# ---------------------------------------------------------------------- #
# JSON helpers
# ---------------------------------------------------------------------- #


def test_filing_json_roundtrip_preserves_all_fields() -> None:
    rec = _record()
    doc = filing_to_json(rec)
    back = filing_from_json(doc)
    assert back == rec


def test_discovery_json_roundtrip() -> None:
    rec = DiscoveryRecord(
        symbol="HDFCBANK", company="HDFC Bank Ltd", isin="INE040A01034",
        period_from=dt.date(2025, 10, 1), period_to=dt.date(2025, 12, 31),
        relating_to="Quarterly", filing_ts=dt.datetime(2026, 1, 12, 17, 45),
        xbrl_url=URL, ixbrl_url=None, attachment_type=None, filing_id="9",
        source=Source.NSE_INTEGRATED, consolidated=True, audited=False,
        cumulative=True, ind_as=True,
    )
    back = discovery_from_json(discovery_to_json(rec))
    assert back == rec


# ---------------------------------------------------------------------- #
# NSE date parsing
# ---------------------------------------------------------------------- #


def test_parse_nse_date_variants() -> None:
    assert parse_nse_date("01-Jan-2019") == dt.date(2019, 1, 1)
    assert parse_nse_date("2026-09-30") == dt.date(2026, 9, 30)
    assert parse_nse_date("31-12-2019") == dt.date(2019, 12, 31)  # DD-MM-YYYY accepted
    assert parse_nse_date(None) is None
    assert parse_nse_date("") is None


def test_parse_nse_ts_variants() -> None:
    assert parse_nse_ts("14-May-2019 18:30:00") == dt.datetime(2019, 5, 14, 18, 30)
    assert parse_nse_ts("14-May-2019 18:30:00 05:30") == dt.datetime(2019, 5, 14, 18, 30)
    # BSE announcement rows use ISO-8601 with a millisecond fraction.
    assert parse_nse_ts("2019-01-07T16:47:05.153") == dt.datetime(
        2019, 1, 7, 16, 47, 5, 153000
    )
    assert parse_nse_ts("2019-01-07T16:47:05") == dt.datetime(2019, 1, 7, 16, 47, 5)
    assert parse_nse_ts(None) is None
    assert parse_nse_ts("not a timestamp") is None


# ---------------------------------------------------------------------- #
# Store
# ---------------------------------------------------------------------- #


def test_store_path_is_deterministic_and_safe(store: PayloadStore) -> None:
    p1 = store.path_for("TCS", "42", URL)
    p2 = store.path_for("TCS", "42", URL)
    assert p1 == p2
    p3 = store.path_for("TCS&M Co", None, URL)
    assert "&" not in str(p3)


def test_store_gzip_roundtrip_is_byte_exact(store: PayloadStore) -> None:
    body = bytes(range(256)) * 41  # includes gzip-hostile bytes, ~10 KB
    path, digest, nbytes = store.write(body, "SYM", "1", URL)
    assert nbytes == len(body)
    assert store.read_raw(path) == body
    assert digest == __import__("hashlib").sha256(body).hexdigest()


def test_store_verify_detects_tampering(store: PayloadStore) -> None:
    body = b"xbrl-payload"
    path, digest, _ = store.write(body, "SYM", "1", URL)
    assert store.verify(path, digest)
    assert not store.verify(path, "0" * 64)


# ---------------------------------------------------------------------- #
# Taxonomy packages
# ---------------------------------------------------------------------- #


def test_vendored_sebi_package_is_discovered() -> None:
    from india_xbrl.taxonomy import TaxonomyRegistry

    packages = TaxonomyRegistry().packages()
    assert packages, "vendored SEBI package must be discovered"
    pkg = packages[0]
    assert pkg.publisher == "Securities and Exchange Board of India"
    assert pkg.identifier == "http://www.sebi.gov.in/xbrl/2025-01-31"
    assert any("in-capmkt-ent-2025-01-31.xsd" in ep for ep in pkg.entry_points)
    assert pkg.rewrite_prefix == "http://www.sebi.gov.in/xbrl/2025-01-31/"


def test_vendored_package_resolves_relative_schema_ref() -> None:
    from india_xbrl.taxonomy import TaxonomyRegistry

    registry = TaxonomyRegistry()
    resolved = registry.resolve("in-capmkt-ent-2025-01-31.xsd")
    assert resolved is not None
    assert resolved.name == "Integrated Filing Finance Taxonomy"


def test_vendored_package_resolves_newer_dated_schema_ref() -> None:
    """Real 2026 filings reference in-capmkt-ent-2026-01-31.xsd; the vendored
    package is dated 2025-01-31. Date substitution must still resolve."""
    from india_xbrl.taxonomy import TaxonomyRegistry

    resolved = TaxonomyRegistry().resolve("in-capmkt-ent-2026-01-31.xsd")
    assert resolved is not None
    assert resolved.publication_date == "2025-01-31"


def test_package_validation_report_mentions_publisher() -> None:
    from india_xbrl.taxonomy import validate_packages

    lines = validate_packages()
    assert any("Securities and Exchange Board of India" in ln for ln in lines)


@pytest.mark.parametrize(
    ("years", "start", "end"),
    [
        ("2019", (2019, 1, 1), (2019, 12, 31)),
        ("2015-2026", (2015, 1, 1), (2026, 12, 31)),
        ("FY2015-16", (2015, 4, 1), (2016, 3, 31)),
        ("2015-16", (2015, 4, 1), (2016, 3, 31)),
    ],
)
def test_years_parsing(years: str, start: tuple[int, int, int], end: tuple[int, int, int]) -> None:
    from india_xbrl.cli import _parse_years

    assert _parse_years(years) == (
        dt.date(*start),
        dt.date(*end),
    )
