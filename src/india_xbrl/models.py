"""Typed record models shared by discovery, fetch, and audit."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import Enum


class FetchStatus(str, Enum):
    """Every discovery row ends in exactly one of these recorded outcomes.

    ``no_xbrl_link`` is *not* an error: a large fraction of listed companies
    (small / pre-Ind-AS filers) genuinely have no XBRL for a period.
    """

    OK = "ok"
    NO_XBRL_LINK = "no_xbrl_link"
    SKIPPED_STANDALONE_REDUNDANT = "skipped_standalone_redundant"
    PENDING = "pending"
    FAILED = "failed"


class TaxonomyFamily(str, Enum):
    """Which taxonomy family an instance document uses.

    The in-bse-fin prefix contains hyphens -- a naive [A-Za-z0-9_]+ pattern
    silently matches nothing.
    """

    BSE_FIN = "in-bse-fin"
    CAPMKT = "in-capmkt"
    UNKNOWN = "unknown"


class Source(str, Enum):
    NSE_LEGACY = "nse_legacy_financial_results"
    NSE_INTEGRATED = "nse_integrated_filing_results"
    BSE_ANNOUNCEMENT = "bse_announcement_results"
    BSE_FIN_RESULTS_FEED = "bse_fin_results_feed"


@dataclass(frozen=True)
class DiscoveryRecord:
    """One row returned by an NSE discovery endpoint."""

    symbol: str
    company: str
    isin: str | None
    period_from: dt.date | None
    period_to: dt.date | None
    relating_to: str | None
    filing_ts: dt.datetime | None
    xbrl_url: str | None
    ixbrl_url: str | None
    attachment_type: str | None
    filing_id: str | None
    source: Source
    consolidated: bool
    audited: bool
    cumulative: bool
    ind_as: bool


@dataclass
class FilingRecord:
    """One line of the manifest. This is the checkpoint."""

    # Identity
    symbol: str
    period_end: dt.date | None = None
    filing_id: str | None = None
    isin: str | None = None
    period_from: dt.date | None = None

    # Provenance
    relating_to: str | None = None
    filing_ts: dt.datetime | None = None
    xbrl_url: str | None = None
    source: Source = Source.NSE_LEGACY

    # Classification
    consolidated: bool = False
    audited: bool = False
    cumulative: bool = False
    ind_as: bool = False
    taxonomy_family: TaxonomyFamily = TaxonomyFamily.UNKNOWN

    # Outcome
    fetch_status: FetchStatus = FetchStatus.PENDING
    http_status: int | None = None
    retry_count: int = 0
    error: str | None = None

    # Stored artifact
    raw_path: str | None = None
    raw_sha256: str | None = None
    raw_bytes: int | None = None
    fetched_at: dt.datetime | None = None

    # Internal: source endpoint pagination order, for stable ordering.
    seq: int | None = field(default=None, repr=False)

    def key(self) -> tuple[str, str]:
        """Stable unique key within a run: (symbol, filing_id or url hash)."""
        ident = self.filing_id or self.xbrl_url or ""
        return (self.symbol, ident)


def parse_nse_date(value: str | None) -> dt.date | None:
    """Parse an NSE date string (``01-Jan-2019`` or ``2026-09-30``)."""
    if not value:
        return None
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_nse_ts(value: str | None) -> dt.datetime | None:
    """Parse an exchange timestamp, naive and as reported.

    Handles NSE's ``01-Jan-2019 16:00:00`` (with optional space-padded IST
    marker like `` 05:30``), minute-only stamps like ``20-Feb-2026 17:01``,
    *and* BSE's ISO ``2019-01-07T16:47:05.153`` (optional fraction, ``T``
    separator).
    """
    if not value:
        return None
    text = value.strip().replace("IST", "").strip()
    for fmt in (
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%d-%b-%Y %H:%M",
        "%d-%b-%Y",
    ):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    # NSE appends a space-padded IST marker like " 05:30" on some endpoints;
    # only reach for the strip when the full text failed to parse, so real
    # minute-precision stamps ("20-Feb-2026 17:01") survive intact.
    stripped = re.sub(r"\s+\d{2}:\d{2}$", "", text)
    if stripped != text:
        return parse_nse_ts(stripped)
    return None
