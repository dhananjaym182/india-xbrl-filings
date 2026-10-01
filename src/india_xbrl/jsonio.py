"""Serialization helpers: records <-> manifest JSON lines."""

from __future__ import annotations

import datetime as dt
from typing import Any

from india_xbrl.models import (
    DiscoveryRecord,
    FilingRecord,
    FetchStatus,
    Source,
    TaxonomyFamily,
)

_DATE_FMT = "%Y-%m-%d"
_TS_FMT = "%Y-%m-%dT%H:%M:%S"


def _date_to_str(value: dt.date | None) -> str | None:
    return value.isoformat() if value else None


def _ts_to_str(value: dt.datetime | None) -> str | None:
    return value.strftime(_TS_FMT) if value else None


def _date_from_str(value: Any) -> dt.date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _ts_from_str(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def discovery_to_json(record: DiscoveryRecord) -> dict[str, Any]:
    return {
        "symbol": record.symbol,
        "company": record.company,
        "isin": record.isin,
        "period_from": _date_to_str(record.period_from),
        "period_to": _date_to_str(record.period_to),
        "relating_to": record.relating_to,
        "filing_ts": _ts_to_str(record.filing_ts),
        "xbrl_url": record.xbrl_url,
        "ixbrl_url": record.ixbrl_url,
        "attachment_type": record.attachment_type,
        "filing_id": record.filing_id,
        "source": record.source.value,
        "consolidated": record.consolidated,
        "audited": record.audited,
        "cumulative": record.cumulative,
        "ind_as": record.ind_as,
    }


def discovery_from_json(doc: dict[str, Any]) -> DiscoveryRecord:
    return DiscoveryRecord(
        symbol=str(doc.get("symbol") or ""),
        company=str(doc.get("company") or ""),
        isin=doc.get("isin"),
        period_from=_date_from_str(doc.get("period_from")),
        period_to=_date_from_str(doc.get("period_to")),
        relating_to=doc.get("relating_to"),
        filing_ts=_ts_from_str(doc.get("filing_ts")),
        xbrl_url=doc.get("xbrl_url"),
        ixbrl_url=doc.get("ixbrl_url"),
        attachment_type=doc.get("attachment_type"),
        filing_id=doc.get("filing_id"),
        source=Source(doc.get("source", Source.NSE_LEGACY.value)),
        consolidated=bool(doc.get("consolidated", False)),
        audited=bool(doc.get("audited", False)),
        cumulative=bool(doc.get("cumulative", False)),
        ind_as=bool(doc.get("ind_as", False)),
    )


def filing_to_json(rec: FilingRecord) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "symbol": rec.symbol,
        "period_end": _date_to_str(rec.period_end),
        "period_from": _date_to_str(rec.period_from),
        "filing_id": rec.filing_id,
        "isin": rec.isin,
        "relating_to": rec.relating_to,
        "filing_ts": _ts_to_str(rec.filing_ts),
        "xbrl_url": rec.xbrl_url,
        "source": rec.source.value,
        "consolidated": rec.consolidated,
        "audited": rec.audited,
        "cumulative": rec.cumulative,
        "ind_as": rec.ind_as,
        "taxonomy_family": rec.taxonomy_family.value,
        "fetch_status": rec.fetch_status.value,
        "http_status": rec.http_status,
        "retry_count": rec.retry_count,
        "error": rec.error,
        "raw_path": rec.raw_path,
        "raw_sha256": rec.raw_sha256,
        "raw_bytes": rec.raw_bytes,
        "fetched_at": _ts_to_str(rec.fetched_at),
    }
    if rec.seq is not None:
        doc["seq"] = rec.seq
    return doc


def filing_from_json(doc: dict[str, Any]) -> FilingRecord:
    return FilingRecord(
        symbol=str(doc.get("symbol") or ""),
        period_end=_date_from_str(doc.get("period_end")),
        period_from=_date_from_str(doc.get("period_from")),
        filing_id=doc.get("filing_id"),
        isin=doc.get("isin"),
        relating_to=doc.get("relating_to"),
        filing_ts=_ts_from_str(doc.get("filing_ts")),
        xbrl_url=doc.get("xbrl_url"),
        source=Source(doc.get("source", Source.NSE_LEGACY.value)),
        consolidated=bool(doc.get("consolidated", False)),
        audited=bool(doc.get("audited", False)),
        cumulative=bool(doc.get("cumulative", False)),
        ind_as=bool(doc.get("ind_as", False)),
        taxonomy_family=TaxonomyFamily(doc.get("taxonomy_family", "unknown")),
        fetch_status=FetchStatus(doc.get("fetch_status", FetchStatus.PENDING.value)),
        http_status=doc.get("http_status"),
        retry_count=int(doc.get("retry_count", 0) or 0),
        error=doc.get("error"),
        raw_path=doc.get("raw_path"),
        raw_sha256=doc.get("raw_sha256"),
        raw_bytes=doc.get("raw_bytes"),
        fetched_at=_ts_from_str(doc.get("fetched_at")),
        seq=doc.get("seq"),
    )
