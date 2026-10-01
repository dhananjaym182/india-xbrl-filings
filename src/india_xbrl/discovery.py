"""Discovery of financial-result XBRL filings from NSE.

Two endpoints (verified 2026-10-01, both HTTP 200 with only a browser
User-Agent -- no cookies):

1. Legacy "Financial Results" (FY2015-16 onward)::

       https://www.nseindia.com/api/corporates-financial-results
           ?index=equities&period=Quarterly&from_date=01-01-2019&to_date=31-12-2019

   ``period=Annual`` returns only ~29 records and ``period=Half-Yearly``
   returns 0 -- the useful history comes from ``from_date``/``to_date``, not
   the ``period`` parameter. The API also silently caps a wide date window, so
   we chunk requests into <=90-day windows.

2. New "Integrated Filing -- Financials" (2025+), paginated::

       https://www.nseindia.com/api/integrated-filing-results
           ?index=equities&from_date=01-07-2026&to_date=30-09-2026

   Response carries ``data``/``size``/``page``/``totalCount`` -- page through
   it. There is no ``integrated-filing-announcements`` endpoint (404); if you
   have seen that name somewhere, it is wrong.

Every record carries both ``xbrl`` and ``ixbrl`` fields. **The ``ixbrl`` field
is NOT XBRL**: it points at a rendered HTML table with zero ``ix:`` tags and
zero facts. We read the ``xbrl`` field only -- the fetcher asserts this.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

from india_xbrl.models import (
    DiscoveryRecord,
    Source,
    parse_nse_date,
    parse_nse_ts,
)
from india_xbrl.transport import Transport

NSE_API_BASE = "https://www.nseindia.com/api"
LEGACY_RESULTS_URL = f"{NSE_API_BASE}/corporates-financial-results"
INTEGRATED_RESULTS_URL = f"{NSE_API_BASE}/integrated-filing-results"

#: Historical cutoff: the Integrated Filing taxonomy (in-capmkt) covers 2025+
#: only; earlier years must come from the legacy endpoint.
INTEGRATED_FILING_START = dt.date(2025, 1, 1)

#: Max days per discovery request window. NSE silently truncates wide ranges;
#: 100 days keeps a full quarter in a single request.
_WINDOW_DAYS = 100

#: Hard page cap for the paginated endpoint, as a run-safe guard.
_MAX_PAGES = 10_000


def _query(d: dt.date) -> str:
    """Format a date the way NSE wants it: DD-MM-YYYY."""
    return d.strftime("%d-%m-%Y")


def _date_windows(
    start: dt.date, end: dt.date, max_days: int = _WINDOW_DAYS
) -> Iterator[tuple[dt.date, dt.date]]:
    """Yield inclusive [window_start, window_end] pairs covering start..end."""
    if end < start:
        return
    cursor = start
    one_day = dt.timedelta(days=1)
    while cursor <= end:
        window_end = min(cursor + dt.timedelta(days=max_days - 1), end)
        yield cursor, window_end
        cursor = window_end + one_day


class DiscoveryClient:
    """Discovers filings from both NSE endpoints."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    # ------------------------------------------------------------------ #
    # Legacy endpoint
    # ------------------------------------------------------------------ #

    def discover_legacy(
        self, start: dt.date, end: dt.date, *, index: str = "equities"
    ) -> list[DiscoveryRecord]:
        """Discover filings from the legacy Financial Results endpoint."""
        records: list[DiscoveryRecord] = []
        for win_start, win_end in _date_windows(start, end):
            url = (
                f"{LEGACY_RESULTS_URL}?index={index}"
                f"&period=Quarterly"
                f"&from_date={_query(win_start)}&to_date={_query(win_end)}"
            )
            resp = self._transport.get(url)
            if resp.status_code != 200:
                raise DiscoveryError(
                    f"legacy discovery returned HTTP {resp.status_code} for {url}"
                )
            records.extend(_parse_legacy_payload(resp.content, window=(win_start, win_end)))
        return records

    # ------------------------------------------------------------------ #
    # Integrated (paginated) endpoint
    # ------------------------------------------------------------------ #

    def discover_integrated(
        self, start: dt.date, end: dt.date, *, index: str = "equities"
    ) -> list[DiscoveryRecord]:
        """Discover filings from the paginated Integrated Filing endpoint."""
        start = max(start, INTEGRATED_FILING_START)
        if end < start:
            return []
        records: list[DiscoveryRecord] = []
        for win_start, win_end in _date_windows(start, end):
            page = 0
            while True:
                page += 1
                if page > _MAX_PAGES:  # pragma: no cover - guard rail
                    raise DiscoveryError(
                        f"integrated discovery exceeded {_MAX_PAGES} pages "
                        f"for window {win_start}..{win_end}"
                    )
                url = (
                    f"{INTEGRATED_RESULTS_URL}?index={index}"
                    f"&from_date={_query(win_start)}&to_date={_query(win_end)}"
                    f"&page={page}"
                )
                resp = self._transport.get(url)
                if resp.status_code != 200:
                    raise DiscoveryError(
                        f"integrated discovery returned HTTP {resp.status_code} "
                        f"for {url}"
                    )
                batch, total_count, done = _parse_integrated_payload(
                    resp.content, window=(win_start, win_end), page=page
                )
                records.extend(batch)
                if done or (total_count is not None and len(records) >= total_count):
                    break
        return records

    def discover(self, start: dt.date, end: dt.date) -> list[DiscoveryRecord]:
        """Discover across both endpoints for a date range of filing dates."""
        legacy = self.discover_legacy(start, end)
        integrated = self.discover_integrated(start, end)
        return legacy + integrated


class DiscoveryError(RuntimeError):
    """Raised when a discovery endpoint misbehaves (non-200, malformed JSON)."""


# ---------------------------------------------------------------------- #
# Payload parsing
# ---------------------------------------------------------------------- #


def _extract_json_array(payload: bytes) -> list[dict[str, Any]]:
    """Pull the list of row objects out of a discovery response.

    The legacy endpoint returns a bare JSON array; some NSE endpoints wrap it
    as ``{"data": [...]}``. Be liberal in what we accept.
    """
    import json

    text = payload.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DiscoveryError(f"discovery response is not JSON: {exc}") from exc
    if isinstance(doc, list):
        return [row for row in doc if isinstance(row, dict)]
    if isinstance(doc, dict):
        data = doc.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
    raise DiscoveryError("unexpected discovery payload shape")


def _parse_legacy_payload(
    payload: bytes, window: tuple[dt.date, dt.date]
) -> list[DiscoveryRecord]:
    del window  # kept for signature symmetry / future validation
    records: list[DiscoveryRecord] = []
    for seq, row in enumerate(_extract_json_array(payload)):
        records.append(_record_from_row(row, Source.NSE_LEGACY, seq))
    return records


def _parse_integrated_payload(
    payload: bytes, window: tuple[dt.date, dt.date], page: int
) -> tuple[list[DiscoveryRecord], int | None, bool]:
    """Returns (records, totalCount, done)."""
    import json

    text = payload.decode("utf-8", errors="replace").strip()
    if not text:
        return [], None, True
    doc = json.loads(text)
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        raise DiscoveryError("unexpected integrated-filing payload shape")
    rows = [r for r in doc["data"] if isinstance(r, dict)]
    total = doc.get("totalCount")
    if not isinstance(total, int):
        total = None
    records = [_record_from_row(r, Source.NSE_INTEGRATED, (page - 1) * len(rows) + i)
               for i, r in enumerate(rows)]
    # An empty page is the only in-payload stop signal; the caller compares
    # the cumulative count against totalCount.
    done = len(rows) == 0
    del window
    return records, total, done


_TRUE_VALUES = {"y", "yes", "true", "1", "consolidated", "cons"}
_FALSE_VALUES = {"n", "no", "false", "0", "non-consolidated", "nonconsolidated"}


def _as_bool(value: Any) -> bool:
    """Coerce NSE's mixed classification values to a bool.

    NSE sends booleans as true booleans *and* as words: ``"Audited"`` /
    ``"Un-Audited"``, ``"Consolidated"`` / ``"Non-Consolidated"``,
    ``"Ind-AS New"`` / ``"Ind-AS Old"`` (measured live 2026-10-01 on both
    endpoints). Unknown words default to False except the explicit
    "non-*" / "un-*" negatives, which are unambiguous.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_VALUES:
            return True
        if text in _FALSE_VALUES or text.startswith(("non-", "un-")):
            return False
        # "audited", "ind-as new", any other affirmative word.
        return text != "" and not text.startswith("-")
    return False


def _clean_url(value: Any) -> str | None:
    """Normalize an NSE attachment field into a usable URL, or None.

    NSE's null sentinel leaks through as a literal ``-`` -- sometimes bare,
    sometimes pre-joined into an absolute URL (``.../xbrl/-``, which 404s;
    measured live 2026-10-01). A URL whose final path segment is ``-``
    carries no attachment.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.upper() in {"NA", "N/A", "NULL", "-"}:
        return None
    if text.startswith("http://") or text.startswith("https://"):
        url = text
    elif text.startswith("/"):
        url = f"https://www.nseindia.com{text}"
    else:
        return text
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return None if tail in {"-", ""} else url


def _record_from_row(row: dict[str, Any], source: Source, seq: int) -> DiscoveryRecord:
    xbrl_url = _clean_url(row.get("xbrl"))
    ixbrl_url = _clean_url(row.get("ixbrl"))
    # Integrated rows key periods "qe_Date" / stamps "creation_Date"; legacy
    # rows use "toDate" / "filingDate" (verified live 2026-10-01).
    period_to = parse_nse_date(
        row.get("to") or row.get("periodTo") or row.get("toDate") or row.get("qe_Date")
    )
    filing_ts = parse_nse_ts(
        row.get("filingDateTs") or row.get("creation_Date")
        or row.get("broadCastDate") or row.get("filingDate")
    )
    filing_id = (
        str(row.get("filingId") or row.get("seqNumber") or row.get("seq_Id") or "")
        .strip()
        or None
    )
    return DiscoveryRecord(
        symbol=str(row.get("symbol") or row.get("sm_symbol") or "").strip().upper(),
        company=str(row.get("company") or row.get("companyname") or row.get("sm_name") or "").strip(),
        isin=str(row.get("isin") or row.get("sm_isin") or "").strip() or None,
        period_from=parse_nse_date(row.get("from") or row.get("periodFrom") or row.get("fromDate")),
        period_to=period_to,
        relating_to=str(row.get("relatingTo") or "").strip() or None,
        filing_ts=filing_ts,
        xbrl_url=xbrl_url,
        ixbrl_url=ixbrl_url,
        attachment_type=str(row.get("attach") or row.get("attachementType") or "").strip() or None,
        filing_id=filing_id,
        source=source,
        consolidated=_as_bool(row.get("consolidated") or row.get("consol")),
        audited=_as_bool(row.get("audited")),
        cumulative=_as_bool(row.get("cumulative")),
        ind_as=_as_bool(row.get("indAs") or row.get("ind_as")),
    )
