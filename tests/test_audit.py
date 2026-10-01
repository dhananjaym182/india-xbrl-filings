"""Acceptance test 2: corruption is detected by --audit; plus coverage reporting."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from conftest import make_record

from india_xbrl.audit import run_audit
from india_xbrl.manifest import Manifest
from india_xbrl.models import FilingRecord, FetchStatus, Source, TaxonomyFamily
from india_xbrl.store import PayloadStore, sha256_bytes

BODY = b"<xbrli:xbrl xmlns:xbrli='http://www.xbrl.org/2003/instance'>X</xbrli:xbrl>"


def _store_ok_payload(store: PayloadStore, manifest: Manifest, symbol: str = "TCS",
                      body: bytes = BODY) -> FilingRecord:
    rec = FilingRecord(
        symbol=symbol,
        period_end=dt.date(2021, 3, 31),
        filing_id="9001",
        xbrl_url=f"https://nsearchives.nseindia.com/corporate/xbrl/{symbol}.xml",
        source=Source.NSE_LEGACY,
    )
    path, digest, nbytes = store.write(body, symbol, rec.filing_id, rec.xbrl_url)
    rec.raw_path = str(path)
    rec.raw_sha256 = digest
    rec.raw_bytes = nbytes
    rec.fetch_status = FetchStatus.OK
    rec.fetched_at = dt.datetime(2021, 5, 14, 18, 30)
    manifest.upsert(rec)
    return rec


def test_audit_clean_run(store: PayloadStore) -> None:
    manifest = Manifest()
    _store_ok_payload(store, manifest)
    report = run_audit(manifest, store)
    assert not report.has_integrity_issues()
    assert report.ok == 1
    assert report.status_counts["ok"] == 1
    assert report.symbols == 1


def test_audit_flags_single_byte_mutation(store: PayloadStore) -> None:
    manifest = Manifest()
    rec = _store_ok_payload(store, manifest)
    assert rec.raw_path
    # Read the gzipped file, mutate one byte of the payload, write back.
    import gzip

    raw = store.read_raw(rec.raw_path)
    mutated = bytearray(raw)
    mutated[len(raw) // 2] ^= 0x01  # flip exactly one byte of the payload
    with open(rec.raw_path, "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:
            gz.write(bytes(mutated))
    report = run_audit(manifest, store)
    kinds = {i.kind for i in report.issues}
    assert "checksum_mismatch" in kinds
    assert report.has_integrity_issues()


def test_audit_flags_missing_file(store: PayloadStore, tmp_path: Path) -> None:
    manifest = Manifest()
    rec = _store_ok_payload(store, manifest)
    assert rec.raw_path
    Path(rec.raw_path).unlink()
    report = run_audit(manifest, store)
    kinds = {i.kind for i in report.issues}
    assert "missing_file" in kinds
    assert report.has_integrity_issues()


def test_audit_counts_fetch_status_breakdown(store: PayloadStore) -> None:
    manifest = Manifest()
    _store_ok_payload(store, manifest, "AAA")
    _store_ok_payload(store, manifest, "BBB")
    no_x = FilingRecord(
        symbol="NOX", period_end=dt.date(2021, 3, 31), filing_id="1",
        xbrl_url=None, source=Source.NSE_LEGACY,
        fetch_status=FetchStatus.NO_XBRL_LINK,
    )
    manifest.upsert(no_x)
    report = run_audit(manifest, store)
    assert report.status_counts["ok"] == 2
    assert report.status_counts["no_xbrl_link"] == 1
    assert report.status_counts["failed"] == 0


def test_audit_coverage_by_year_and_symbol(store: PayloadStore) -> None:
    manifest = Manifest()
    _store_ok_payload(store, manifest, "AAA")
    rec = _store_ok_payload(store, manifest, "BBB")
    rec.period_end = dt.date(2022, 12, 31)
    manifest.upsert(rec)
    report = run_audit(manifest, store)
    assert report.coverage_by_year == {2021: 1, 2022: 1}
    assert report.symbols == 2
    assert report.top_symbols[0][1] == 1


def test_audit_counts_orphan_payloads(store: PayloadStore) -> None:
    manifest = Manifest()
    _store_ok_payload(store, manifest, "AAA")
    # An orphan on disk: not referenced by any manifest record.
    store.write(b"<orphan/>", "ORPHAN", "1", "https://example.com/o.xml")
    report = run_audit(manifest, store)
    assert report.orphans_on_disk == 1


def test_audit_reports_taxonomy_packages(store: PayloadStore) -> None:
    manifest = Manifest()
    _store_ok_payload(store, manifest)
    report = run_audit(manifest, store)
    assert report.taxonomy_lines
    assert any("in-capmkt" in line or "taxonomy" in line.lower()
               for line in report.taxonomy_lines)


def test_sha256_is_over_raw_bytes(store: PayloadStore) -> None:
    path, digest, nbytes = store.write(BODY, "SYM", "1", "https://x/y.xml")
    assert digest == sha256_bytes(BODY)
    assert nbytes == len(BODY)
    assert store.read_raw(path) == BODY
