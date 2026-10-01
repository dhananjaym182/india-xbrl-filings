"""BSE discovery: financial-result filings from api.bseindia.com + www feeds.

**Measured 2026-10-01 -- the "BSE is blocked" claim is wrong in an important
way.** api.bseindia.com *is* behind Akamai, but the block is a browser-
fingerprint rule, not a blanket ban:

- naive ``python-requests`` default UA -> 403 on every path;
- a bare Chrome UA -> 403 *on some requests and not others* (the trap that
  fooled earlier audits);
- the full header set in :data:`india_xbrl.transport.BSE_HEADERS` -- Chrome UA
  **plus** ``Accept-Encoding`` (requests sends it by default; ``curl`` does
  not) **plus** the ``sec-ch-ua`` client hints -> HTTP 200, reliably.

Endpoint found by extracting the SPA's own URL map from
``www.bseindia.com/assets/includenew/js/chunk-*.js``::

    /BseIndiaAPI/api/AnnSubCategoryGetData/w
        ?pageno=N &strCat=Result &strPrevDate=YYYYMMDD &strScrip=
        &strSearch=P &strToDate=YYYYMMDD &strType=C &subcategory=-1

- ``strCat=Result`` (the *name*, not the id 7) selects financial results.
- Response: ``{"Table": [rows], "Table1": [{"ROWCNT": total}]}``.
- ``strPrevDate``/``strToDate`` windows work **back to at least Jan 2019**
  (verified), so BSE gives full history without an NSE-style cutoff.
- ~50 rows/page; ``Table1[0].ROWCNT`` is the window total. Under sustained
  backfill load (full-year run, 2026-10-01) BSE also threw one transient 403
  right after a large PDF download and one ``ReadTimeout`` at 30 s -- so
  discovery GETs here use a longer timeout plus bounded retries with
  backoff, and fail loudly (``BseDiscoveryError``) when exhausted.

Result rows carry ``ATTACHMENTNAME`` (the PDF) but **no XBRL instance link**.
Two complementary paths to the instances:

1. **RSS feed** (Integrated Filing / IFIndAs era): ``GET
   https://www.bseindia.com/Data/XML/FinancialResultsFeed.aspx`` -- a small
   RSS document (fixed "latest" window; no working pagination parameters).
   Each ``<item>`` links into
   ``https://www.bseindia.com/XBRLFILES/IFIndasDuplicateUploadDocument/``:
   the ``.html`` document is **iXBRL** (``xmlns:in-capmkt`` + hidden ``ix:``
   facts), while the ``.xml`` *sibling* is the plain instance our reader
   accepts -- the inverse of NSE's ixbrl trap. We map ``.html`` -> ``.xml``
   for ``xbrl_url`` and keep the original in ``ixbrl_url``.

2. **Announcement attachments** (legacy era): Result rows carry ``NEWSID`` +
   ``SCRIP_CD`` and an ``ATTACHMENTNAME`` served from
   ``/xml-data/corpfiling/AttachLive/`` (verified: a 9.4 MB result PDF).
   Where no attachment exists the row is still indexed with
   ``fetch_status=no_xbrl_link`` -- the same recorded-outcome semantics as
   NSE. (Legacy ``XBRLFILES/CGXBRLDataXML/ANN_*.xml`` paths are dead -- 404
   for 2019 rows; do not resurrect them.)

Revisions are preserved exactly as on NSE: multiple filings for the same
company + period (standalone/consolidated/audited/unaudited) each become
their own manifest row keyed by NEWSID.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from india_xbrl.models import DiscoveryRecord, Source, parse_nse_date, parse_nse_ts
from india_xbrl.transport import Response, Transport

logger = logging.getLogger(__name__)

try:  # requests is a hard dependency; the guard only helps tooling
    import requests as _requests

    _NETWORK_ERRORS: tuple[type[BaseException], ...] = (
        _requests.RequestException,
        OSError,
    )
except ImportError:  # pragma: no cover
    _NETWORK_ERRORS = (OSError,)

BSE_API_BASE = "https://api.bseindia.com/BseIndiaAPI/api"
BSE_ANN_SUBCATEGORY = f"{BSE_API_BASE}/AnnSubCategoryGetData/w"
BSE_FIN_RESULTS_RSS = "https://www.bseindia.com/Data/XML/FinancialResultsFeed.aspx"
BSE_ATTACHMENT_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive"

#: BSE caps announcement windows; ~7 days is well inside the limit.
_WINDOW_DAYS = 7

#: Page-size hint observed on AnnSubCategoryGetData (~50 rows).
_EXPECTED_PAGE_ROWS = 50

_MAX_PAGES = 5_000  # run-safe guard

#: BSE's API slows under sustained sequential load (a 30 s ReadTimeout was
#: observed mid-backfill), so discovery GETs get extra headroom.
_DISCOVERY_TIMEOUT = 45.0

#: Attempts per discovery GET (1 initial + bounded retries for transient
#: network errors). Exhaustion raises BseDiscoveryError -- never a silent skip.
_DISCOVERY_ATTEMPTS = 3

#: Linear backoff between discovery retries.
_RETRY_SLEEP_SECONDS = 3.0


class BseDiscoveryError(RuntimeError):
    """Raised when a BSE endpoint misbehaves (non-200, malformed JSON)."""


class BseDiscoveryClient:
    """Discovers financial-result filings from BSE."""

    def __init__(
        self,
        transport: Transport,
        *,
        attempts: int = _DISCOVERY_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._attempts = max(1, attempts)
        self._sleep = sleep

    # ------------------------------------------------------------------ #

    def _get(self, url: str) -> Response:
        """GET with bounded retries for transient network errors."""
        last: BaseException | None = None
        for attempt in range(self._attempts):
            try:
                return self._transport.get(url, timeout=_DISCOVERY_TIMEOUT)
            except _NETWORK_ERRORS as exc:
                last = exc
                if attempt + 1 < self._attempts:
                    delay = _RETRY_SLEEP_SECONDS * (attempt + 1)
                    logger.warning(
                        "BSE GET failed (%s: %s), retrying in %.0fs: %s",
                        type(exc).__name__,
                        exc,
                        delay,
                        url,
                    )
                    self._sleep(delay)
        raise BseDiscoveryError(
            f"BSE request failed after {self._attempts} attempts: {last}"
        ) from last

    def discover_results(
        self, start: dt.date, end: dt.date, *, category: str = "Result"
    ) -> list[DiscoveryRecord]:
        """Discover financial-result announcements in [start, end] (inclusive).

        Paged, windowed, deterministic: the URL for a given
        (window, page) pair is always identical, so an index run can be
        reproduced. Windows never extend past ``end`` -- the last window is
        clipped, keeping the URL set stable.
        """
        records: list[DiscoveryRecord] = []
        one_day = dt.timedelta(days=1)
        cursor = start
        while cursor <= end:
            win_end = min(cursor + dt.timedelta(days=_WINDOW_DAYS - 1), end)
            before = len(records)
            records.extend(
                self._discover_window(cursor, win_end, category=category)
            )
            logger.info(
                "BSE window %s..%s: +%d rows (total %d)",
                cursor, win_end, len(records) - before, len(records),
            )
            cursor = win_end + one_day
        return records

    def discover_integrated(self) -> list[DiscoveryRecord]:
        """Discover Integrated-Filing (IFIndAs) instances from the RSS feed.

        The feed is a fixed "latest" snapshot; use it for incremental sync
        (run daily) or as a cross-check. Full backfill comes from
        :meth:`discover_results`.
        """
        resp = self._get(BSE_FIN_RESULTS_RSS)
        if resp.status_code != 200:
            raise BseDiscoveryError(
                f"financial-results feed returned HTTP {resp.status_code}"
            )
        return _parse_fin_results_rss(resp.content)

    # ------------------------------------------------------------------ #

    def _discover_window(
        self, win_start: dt.date, win_end: dt.date, *, category: str
    ) -> list[DiscoveryRecord]:
        records: list[DiscoveryRecord] = []
        page = 0
        while True:
            page += 1
            if page > _MAX_PAGES:  # pragma: no cover - guard rail
                raise BseDiscoveryError(
                    f"BSE discovery exceeded {_MAX_PAGES} pages for "
                    f"{win_start}..{win_end}"
                )
            url = _window_url(page, win_start, win_end, category=category)
            resp = self._get(url)
            if resp.status_code != 200:
                # Akamai 403s are fingerprint regressions, not per-row
                # outcomes: fail loudly so the operator notices.
                raise BseDiscoveryError(
                    f"BSE announcements returned HTTP {resp.status_code} for {url}"
                )
            rows, total = _parse_ann_subcategory(resp.content)
            base = (page - 1) * _EXPECTED_PAGE_ROWS
            for i, row in enumerate(rows):
                records.append(
                    _record_from_row(row, Source.BSE_ANNOUNCEMENT, base + i)
                )
            fetched = page * _EXPECTED_PAGE_ROWS
            if not rows or total is None or fetched >= total:
                break
        return records


# ---------------------------------------------------------------------- #
# Payload parsing
# ---------------------------------------------------------------------- #


def _window_url(
    page: int, win_start: dt.date, win_end: dt.date, *, category: str
) -> str:
    return (
        f"{BSE_ANN_SUBCATEGORY}?pageno={page}"
        f"&strCat={category}"
        f"&strPrevDate={win_start.strftime('%Y%m%d')}"
        f"&strScrip=&strSearch=P"
        f"&strToDate={win_end.strftime('%Y%m%d')}"
        f"&strType=C&subcategory=-1"
    )


def _extract_json(payload: bytes) -> dict[str, Any]:
    try:
        doc = json.loads(payload.decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BseDiscoveryError(f"BSE response is not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise BseDiscoveryError("unexpected BSE payload shape")
    return doc


def _parse_ann_subcategory(payload: bytes) -> tuple[list[dict[str, Any]], int | None]:
    """Split an AnnSubCategoryGetData page into (rows, window-total)."""
    doc = _extract_json(payload)
    rows = [r for r in doc.get("Table", []) if isinstance(r, dict)]
    total: int | None = None
    table1 = doc.get("Table1")
    if isinstance(table1, list) and table1 and isinstance(table1[0], dict):
        raw = table1[0].get("ROWCNT")
        if isinstance(raw, int):
            total = raw
    return rows, total


_DATE_RE = re.compile(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})")
_PERIOD_ENDED_RE = re.compile(
    r"(?:for|ended|quarter ended|year ended)[^0-9]{0,20}"
    r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})",
    re.I,
)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH_NAME_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(\d{1,2}),?\s+(\d{4})(?!\d)",
    re.I,
)

#: A result's period cannot end more than a year from today. BSE subjects
#: occasionally carry year typos -- measured live 2026-10-01: "Quarter Ended
#: June 30, 3036" (TTK Prestige) and "Dec 31, 20925" (Goenka Diamond). A
#: well-formed but implausible date is refused (recorded as None), never
#: second-guessed into a "corrected" value -- that would be silent corruption.
_MAX_FUTURE_PERIOD = dt.timedelta(days=366)


def _period_end_from_subject(subject: str | None) -> dt.date | None:
    """Best-effort period_end from the announcement subject line.

    Result subjects embed the period in several styles: ``... For The Quarter
    Ended 31/12/2018.``, ``... For Period Ended March 31, 2026``. Numeric
    "ended" phrases win, then any month-name date, then any dd/mm/yyyy date.
    Implausible dates (year typos in BSE subjects) yield None.
    """
    if not subject:
        return None
    numeric = _PERIOD_ENDED_RE.search(subject) or _DATE_RE.search(subject)
    if numeric is not None:
        day, month, year = (int(g) for g in numeric.groups())
    else:
        named = _MONTH_NAME_RE.search(subject)
        if named is None:
            return None
        month = _MONTHS.index(named.group(1).lower()) + 1
        day, year = int(named.group(2)), int(named.group(3))
    try:
        candidate = dt.date(year, month, day)
    except ValueError:
        return None
    if candidate > dt.date.today() + _MAX_FUTURE_PERIOD:
        return None
    return candidate


_CONSOLIDATED_RE = re.compile(r"consolidated", re.I)
_AUDITED_RE = re.compile(r"\baudited\b", re.I)
_UNAUDITED_RE = re.compile(r"un-?audited", re.I)
_IND_AS_RE = re.compile(r"ind\s*-?\s*as", re.I)


def _record_from_row(row: dict[str, Any], source: Source, seq: int) -> DiscoveryRecord:
    subject = row.get("NEWSSUB") or row.get("HEADLINE")
    subject_text = str(subject) if subject else ""
    filing_id = str(row.get("NEWSID") or "").strip() or None
    scrip = row.get("SCRIP_CD")
    symbol = f"BSE-{scrip}" if scrip is not None else ""
    attachment = str(row.get("ATTACHMENTNAME") or "").strip() or None
    attachment_url = f"{BSE_ATTACHMENT_BASE}/{attachment}" if attachment else None
    return DiscoveryRecord(
        symbol=symbol,
        company=str(row.get("SLONGNAME") or "").strip(),
        isin=None,
        period_from=None,
        period_to=_period_end_from_subject(subject_text),
        relating_to=str(row.get("SUBCATNAME") or "").strip() or None,
        filing_ts=parse_nse_ts(str(row.get("NEWS_DT") or "") or None),
        xbrl_url=attachment_url,  # may be None -> no_xbrl_link is allowed
        ixbrl_url=None,
        attachment_type=str(row.get("ATTACHMENTNAME") or "").rsplit(".", 1)[-1] or None,
        filing_id=filing_id,
        source=source,
        consolidated=bool(_CONSOLIDATED_RE.search(subject_text)),
        audited=bool(_AUDITED_RE.search(subject_text))
        and not bool(_UNAUDITED_RE.search(subject_text)),
        cumulative=False,
        ind_as=bool(_IND_AS_RE.search(subject_text)),
    )


_RSS_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
_SCRIP_IN_TITLE_RE = re.compile(r"\((\d{6})\)\s*$")
_INSTANCE_EXT_RE = re.compile(r"\.(xml|html)$", re.I)


def _parse_fin_results_rss(payload: bytes) -> list[DiscoveryRecord]:
    """Parse the FinancialResultsFeed RSS into discovery records.

    Each item links into the IFIndas upload tree. The ``.html`` document is
    iXBRL; its ``.xml`` sibling is the plain instance, so ``.html`` links are
    remapped to the sibling and the original kept as ``ixbrl_url``. Items
    already linking to ``.xml`` keep it as both link and id source.
    """
    text = payload.decode("utf-8-sig", errors="replace")
    records: list[DiscoveryRecord] = []
    seen: set[tuple[str, str]] = set()
    for item in _RSS_ITEM_RE.findall(text):
        title = _rss_field(item, "title") or ""
        link = _rss_field(item, "link") or ""
        desc = _rss_field(item, "description") or ""
        if not link:
            continue

        # Scrip code from the title, e.g. "Impex Ferro Tech Ltd (532614)".
        scrip_m = _SCRIP_IN_TITLE_RE.search(title.strip())
        scrip = scrip_m.group(1) if scrip_m else None

        instance_url = _xml_sibling(link)
        # The feed repeats exact rows (observed live 2026-10-01); a repeated
        # (symbol, instance) is noise, while a *changed* instance for the same
        # symbol is a distinct filing and is kept.
        dedupe_key = (f"BSE-{scrip}" if scrip else "", instance_url)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        desc_l = desc.lower()
        records.append(
            DiscoveryRecord(
                symbol=f"BSE-{scrip}" if scrip else "",
                company=re.sub(r"\s*\(\d{6}\)\s*$", "", title).strip(),
                isin=None,
                period_from=parse_nse_date(_find_kv_date(desc, "PERIOD START DATE")),
                period_to=parse_nse_date(_find_kv_date(desc, "PERIOD END DATE")),
                relating_to="Integrated Filing",
                filing_ts=None,
                xbrl_url=instance_url if instance_url.lower().endswith(".xml") else None,
                ixbrl_url=link if link.lower().endswith(".html") else None,
                attachment_type="XBRL",
                filing_id=_filing_id_from_instance(instance_url),
                source=Source.BSE_FIN_RESULTS_FEED,
                consolidated="consolidated" in desc_l,
                audited="audited" in desc_l and "unaudited" not in desc_l,
                cumulative=False,
                ind_as="ind_as" in desc_l or "ind as" in desc_l,
            )
        )
    return records


def _rss_field(item: str, name: str) -> str | None:
    m = re.search(rf"<{name}>(.*?)</{name}>", item, re.S)
    return m.group(1).strip() if m else None


def _xml_sibling(url: str) -> str:
    """Map a ``.html`` link to its ``.xml`` sibling; anything else unchanged."""
    if url.lower().endswith(".html"):
        return url[:-5] + ".xml"
    return url


def _find_kv_date(desc: str, key: str) -> str | None:
    m = re.search(rf"{re.escape(key)}\s*:\s*([\d\-]+)", desc, re.I)
    return m.group(1) if m else None


def _filing_id_from_instance(instance_url: str) -> str | None:
    tail = instance_url.rstrip("/").rsplit("/", 1)[-1]
    tail = _INSTANCE_EXT_RE.sub("", tail)
    return tail or None
