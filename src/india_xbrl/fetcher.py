"""Resumable fetcher: download XBRL payloads with recorded outcomes.

Five commitments kept from the reference implementation:

1. Raw payloads preserved byte-exact, gzipped, SHA-256 recorded.
2. The manifest IS the checkpoint; resume = skip ``ok`` records whose file
   still exists and hash-matches. Killing the process loses at most the
   in-flight filing.
3. Revisions preserved, never collapsed (distinct manifest keys per filing).
4. Pacing with escalating per-attempt backoff and random jitter; each worker
   thread enforces its own spacing so sleeps are never shared racy state.
5. Every failure is a recorded outcome (``fetch_status``), not an exception.

**The fetcher reads the ``xbrl`` field only.** Discovery records also carry an
``ixbrl`` field pointing at rendered HTML with zero XBRL facts; downloading it
wastes bandwidth and silently produces an empty parse. :class:`FetchPlan`
carries no ixbrl URL by construction.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from india_xbrl.manifest import Manifest
from india_xbrl.models import (
    DiscoveryRecord,
    FilingRecord,
    FetchStatus,
    Source,
)
from india_xbrl.pacing import Pacer
from india_xbrl.store import PayloadStore
from india_xbrl.transport import (
    DEFAULT_BACKOFF_BASE,
    DEFAULT_MIN_INTERVAL,
    Clock,
    RealClock,
    Transport,
)

logger = logging.getLogger(__name__)

#: Default concurrency. NSE holds connections ~30 s, so throughput scales with
#: workers, not with a shorter delay. Raising this is a *politeness* decision,
#: not a performance one (see README).
DEFAULT_WORKERS = 12

#: Total attempts per filing (1 initial + retries with escalating backoff).
MAX_ATTEMPTS = 4


@dataclass(frozen=True)
class FetchPlan:
    """What the fetcher will download.

    Carries the ``xbrl`` URL and *no* ixbrl URL: the ixbrl attachment is a
    rendered HTML table, not XBRL, and must never be fetched as a payload.
    """

    symbol: str
    filing_id: str | None
    xbrl_url: str | None
    period_end: dt.date | None = None
    consolidated: bool = False
    audited: bool = False
    cumulative: bool = False
    ind_as: bool = False
    source: Source = Source.NSE_LEGACY

    def key(self) -> tuple[str, str]:
        return (self.symbol, self.filing_id or self.xbrl_url or "")


def plan_from_discovery(rec: DiscoveryRecord) -> FetchPlan:
    """Build a fetch plan from a discovery record.

    Deliberately reads ``rec.xbrl_url`` only. ``rec.ixbrl_url`` exists on the
    record for provenance but is never planned for download -- asserted by
    ``tests/test_fetcher.py::test_fetcher_reads_xbrl_field_only``.
    """
    return FetchPlan(
        symbol=rec.symbol,
        filing_id=rec.filing_id,
        xbrl_url=rec.xbrl_url,
        period_end=rec.period_to,
        consolidated=rec.consolidated,
        audited=rec.audited,
        cumulative=rec.cumulative,
        ind_as=rec.ind_as,
        source=rec.source,
    )


@dataclass
class FetchResult:
    status: FetchStatus
    payload: bytes | None = None
    http_status: int | None = None
    retry_count: int = 0
    error: str | None = None


@dataclass
class FetchStats:
    total: int = 0
    downloaded: int = 0
    skipped_already_ok: int = 0
    no_xbrl_link: int = 0
    skipped_standalone: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "downloaded": self.downloaded,
            "skipped_already_ok": self.skipped_already_ok,
            "no_xbrl_link": self.no_xbrl_link,
            "skipped_standalone": self.skipped_standalone,
            "failed": self.failed,
        }


class Fetcher:
    """Downloads payloads according to the plan, resuming from the manifest."""

    def __init__(
        self,
        transport: Transport,
        store: PayloadStore,
        manifest: Manifest,
        *,
        clock: Clock | None = None,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        workers: int = DEFAULT_WORKERS,
        max_attempts: int = MAX_ATTEMPTS,
        persist: Callable[[], None] | None = None,
    ) -> None:
        self._transport = transport
        self._store = store
        self._manifest = manifest
        self._clock = clock or RealClock()
        self._min_interval = min_interval
        self._backoff_base = backoff_base
        self._workers = max(1, workers)
        self._max_attempts = max(1, max_attempts)
        self._persist = persist
        self._pacers: dict[int, Pacer] = {}
        self._pacer_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def run(
        self,
        plans: list[FetchPlan],
        *,
        save_every: int = 50,
        skip_standalone_redundant: bool = True,
    ) -> FetchStats:
        """Fetch all plans. Resumable: verified ``ok`` records are skipped.

        ``skip_standalone_redundant`` skips standalone filings for
        symbol+period pairs where a consolidated filing exists in the index.
        Both rows remain in the manifest with distinct ``fetch_status`` values
        -- nothing is silently dropped; pass ``False`` to download everything.
        """
        stats = FetchStats(total=len(plans))
        pending: list[FetchPlan] = []

        for plan in plans:
            filing = self._ensure_record(plan)
            status = filing.fetch_status
            if status == FetchStatus.OK and self._verify_existing(filing):
                stats.skipped_already_ok += 1
                continue
            if status == FetchStatus.NO_XBRL_LINK:
                # Final outcome, not an error: this company/period has no XBRL.
                stats.no_xbrl_link += 1
                continue
            if (
                skip_standalone_redundant
                and status == FetchStatus.SKIPPED_STANDALONE_REDUNDANT
            ):
                stats.skipped_standalone += 1
                continue
            pending.append(plan)

        consolidated = self._consolidated_index()
        to_fetch: list[FetchPlan] = []
        for plan in pending:
            filing = self._ensure_record(plan)
            if (
                skip_standalone_redundant
                and not filing.consolidated
                and filing.period_end is not None
                and (filing.symbol, filing.period_end) in consolidated
            ):
                filing.fetch_status = FetchStatus.SKIPPED_STANDALONE_REDUNDANT
                stats.skipped_standalone += 1
                continue
            to_fetch.append(plan)

        logger.info(
            "fetch: %d planned, %d already ok (verified), %d no_xbrl_link, "
            "%d skipped standalone, %d to fetch",
            stats.total,
            stats.skipped_already_ok,
            stats.no_xbrl_link,
            stats.skipped_standalone,
            len(to_fetch),
        )

        applied = 0
        if to_fetch:
            if self._workers == 1:
                for plan in to_fetch:
                    result = self._fetch_one(plan)
                    self._apply(plan, result)
                    applied += 1
                    self._maybe_save(applied, save_every)
            else:
                with ThreadPoolExecutor(max_workers=self._workers) as pool:
                    futures = {pool.submit(self._fetch_one, p): p for p in to_fetch}
                    for future in as_completed(futures):
                        plan = futures[future]
                        self._apply(plan, future.result())
                        applied += 1
                        self._maybe_save(applied, save_every)

        if applied:
            self._save()
        self._count(stats)
        return stats

    # ------------------------------------------------------------------ #
    # Fetch mechanics
    # ------------------------------------------------------------------ #

    def _fetch_one(self, plan: FetchPlan) -> FetchResult:
        if not plan.xbrl_url:
            # no_xbrl_link is the correct answer for a company that does not
            # file XBRL. Record it; never retry it.
            return FetchResult(status=FetchStatus.NO_XBRL_LINK, retry_count=0)

        pacer = self._pacer()
        last_error: str | None = None
        retry_count = 0
        for attempt in range(self._max_attempts):
            if attempt > 0:
                # Escalating backoff with jitter, per attempt. Without the
                # jitter, concurrent workers retry in lockstep and re-trigger
                # the rate limit together.
                pacer.backoff_sleep(attempt - 1)
            else:
                pacer.wait_before_request()
            try:
                resp = self._transport.get(plan.xbrl_url)
            except Exception as exc:  # transport-level failure
                last_error = f"transport error: {exc}"
                retry_count = attempt + 1
                continue
            if resp.status_code == 200 and resp.content:
                return FetchResult(
                    status=FetchStatus.OK,
                    payload=resp.content,
                    http_status=resp.status_code,
                    retry_count=retry_count,
                )
            if resp.status_code == 404:
                # A missing instance is a recorded outcome, not a crash.
                return FetchResult(
                    status=FetchStatus.FAILED,
                    http_status=404,
                    error="HTTP 404 for xbrl url",
                    retry_count=retry_count,
                )
            last_error = f"HTTP {resp.status_code}"
            retry_count = attempt + 1
        return FetchResult(
            status=FetchStatus.FAILED,
            error=last_error,
            retry_count=retry_count,
        )

    def _apply(self, plan: FetchPlan, result: FetchResult) -> None:
        filing = self._ensure_record(plan)
        filing.http_status = result.http_status
        filing.retry_count = result.retry_count
        filing.error = result.error
        filing.fetch_status = result.status
        if result.status == FetchStatus.OK and result.payload is not None:
            path, digest, nbytes = self._store.write(
                result.payload, plan.symbol, plan.filing_id, plan.xbrl_url
            )
            filing.raw_path = str(path)
            filing.raw_sha256 = digest
            filing.raw_bytes = nbytes
            filing.fetched_at = dt.datetime.now()
            logger.debug("stored %s (%d bytes)", path, nbytes)

    # ------------------------------------------------------------------ #
    # Manifest helpers
    # ------------------------------------------------------------------ #

    def _ensure_record(self, plan: FetchPlan) -> FilingRecord:
        key = plan.key()
        filing = self._manifest.get(key)
        if filing is None:
            filing = FilingRecord(
                symbol=plan.symbol,
                period_end=plan.period_end,
                filing_id=plan.filing_id,
                xbrl_url=plan.xbrl_url,
                source=plan.source,
                consolidated=plan.consolidated,
                audited=plan.audited,
                cumulative=plan.cumulative,
                ind_as=plan.ind_as,
            )
            self._manifest.upsert(filing)
        return filing

    def _consolidated_index(self) -> set[tuple[str, dt.date]]:
        """(symbol, period_end) pairs that have a consolidated filing."""
        return {
            (r.symbol, r.period_end)
            for r in self._manifest.records
            if r.consolidated and r.period_end is not None
        }

    def _verify_existing(self, filing: FilingRecord) -> bool:
        """Resume check: file exists AND raw SHA-256 still matches."""
        if not filing.raw_path or not filing.raw_sha256:
            return False
        return self._store.verify(filing.raw_path, filing.raw_sha256)

    def _maybe_save(self, applied: int, save_every: int) -> None:
        if save_every > 0 and applied % save_every == 0:
            self._save()

    def _save(self) -> None:
        if self._persist is not None:
            self._persist()

    # ------------------------------------------------------------------ #
    # Per-thread pacing
    # ------------------------------------------------------------------ #

    def _pacer(self) -> Pacer:
        """One Pacer per worker thread: spacing is per-connection state."""
        ident = threading.get_ident()
        with self._pacer_lock:
            pacer = self._pacers.get(ident)
            if pacer is None:
                pacer = Pacer(
                    self._clock,
                    min_interval=self._min_interval,
                    backoff_base=self._backoff_base,
                    rng=random.Random(),
                )
                self._pacers[ident] = pacer
        return pacer

    def _count(self, stats: FetchStats) -> None:
        """Reconcile final stats with manifest state (single source of truth)."""
        counts = self._manifest.status_counts()
        stats.downloaded = counts[FetchStatus.OK]
        stats.no_xbrl_link = counts[FetchStatus.NO_XBRL_LINK]
        stats.skipped_standalone = counts[FetchStatus.SKIPPED_STANDALONE_REDUNDANT]
        stats.failed = counts[FetchStatus.FAILED]
