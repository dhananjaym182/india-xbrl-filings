"""CLI: --index / --fetch / --audit with exchange and source selection.

::

    india-xbrl --index --source nse  --years 2015-2026        # NSE discovery
    india-xbrl --index --source bse  --years 2019-2026        # BSE discovery
    india-xbrl --index --source all  --years 2019-2026        # both
    india-xbrl --fetch  [--limit N] [--symbols ...]           # resumable fetch
    india-xbrl --audit                                        # offline report

``--source`` is sticky in the on-disk index: each index run appends its
records and records which source produced them, so mixed corpora are safe.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import sys
from pathlib import Path

from india_xbrl import __version__
from india_xbrl.audit import run_audit
from india_xbrl.bse import BseDiscoveryClient, BseDiscoveryError
from india_xbrl.discovery import DiscoveryClient, DiscoveryError
from india_xbrl.fetcher import (
    DEFAULT_WORKERS,
    FetchPlan,
    Fetcher,
    plan_from_discovery,
)
from india_xbrl.manifest import DiscoveryIndex, Manifest
from india_xbrl.models import DiscoveryRecord, FilingRecord, FetchStatus, Source
from india_xbrl.store import PayloadStore
from india_xbrl.transport import RequestsTransport

logger = logging.getLogger("india_xbrl")

DEFAULT_DATA_DIR = Path("data")


def _parse_years(years: str) -> tuple[dt.date, dt.date]:
    """Parse ``2015-2026`` (calendar), or ``2015-16`` / ``FY2015-16``
    (Indian fiscal year, Apr-Mar) into an inclusive date range."""
    text = years.strip().upper().replace("FY", "")
    m = re.fullmatch(r"(\d{4})(?:-(\d{2,4}))?", text)
    if not m:
        raise SystemExit(
            f"invalid --years {years!r}: use 2015-2026 (calendar) or 2015-16 (fiscal)"
        )
    y1 = int(m.group(1))
    y2_raw = m.group(2)
    if y2_raw is None:
        return dt.date(y1, 1, 1), dt.date(y1, 12, 31)
    if len(y2_raw) == 2:
        # Two-digit suffix means Indian fiscal-year style (FY2015-16):
        # April of y1 through March of y2.
        y2 = int(f"{str(y1)[:2]}{y2_raw}")
        return dt.date(y1, 4, 1), dt.date(y2, 3, 31)
    y2 = int(y2_raw)
    if y2 < y1:
        return dt.date(y1, 4, 1), dt.date(y2, 3, 31)
    return dt.date(y1, 1, 1), dt.date(y2, 12, 31)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="india-xbrl",
        description="Discover and download NSE/BSE financial-result XBRL filings.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--index",
        action="store_true",
        help="discover filings and write the index (network)",
    )
    mode.add_argument(
        "--fetch",
        action="store_true",
        help="download XBRL payloads per the index (network, resumable)",
    )
    mode.add_argument(
        "--audit",
        action="store_true",
        help="read-only report: coverage, checksums, gaps (no network)",
    )

    net = parser.add_argument_group("index/fetch options")
    net.add_argument(
        "--source",
        choices=("nse", "bse", "all"),
        default="nse",
        help="exchange(s) to discover from (default: nse)",
    )
    net.add_argument(
        "--years",
        default="2015-2026",
        help="year range, e.g. 2015-2026 (calendar) or 2015-16 (fiscal)",
    )
    net.add_argument(
        "--limit", type=int, default=None, help="fetch at most N filings this run"
    )
    net.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="restrict to these symbols (used with --fetch)",
    )
    net.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"concurrent download workers (default {DEFAULT_WORKERS}; raising "
        "this is a politeness decision, see README)",
    )
    net.add_argument(
        "--no-skip-standalone",
        action="store_true",
        help="download standalone filings even when a consolidated filing "
        "exists for the same symbol+period",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="directory for index.jsonl / manifest.jsonl / payloads",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def _paths(data_dir: Path) -> tuple[Path, Path, Path]:
    return data_dir / "index.jsonl", data_dir / "manifest.jsonl", data_dir / "payloads"


def _discover(
    source: str, transport: RequestsTransport, start: dt.date, end: dt.date
) -> list[DiscoveryRecord]:
    records: list[DiscoveryRecord] = []
    if source in ("nse", "all"):
        records.extend(DiscoveryClient(transport).discover(start, end))
    if source in ("bse", "all"):
        bse = BseDiscoveryClient(transport)
        records.extend(bse.discover_results(start, end))
        records.extend(bse.discover_integrated())
    return records


def cmd_index(args: argparse.Namespace) -> int:
    start, end = _parse_years(args.years)
    index_path, manifest_path, _ = _paths(args.data_dir)
    logger.info("discovering %s filings %s .. %s", args.source, start, end)
    transport = RequestsTransport()
    try:
        records = _discover(args.source, transport, start, end)
    except (DiscoveryError, BseDiscoveryError) as exc:
        logger.error("discovery failed: %s", exc)
        return 3
    finally:
        transport.close()

    logger.info("discovered %d filings", len(records))
    index = DiscoveryIndex.load(index_path)
    index.records.extend(records)
    index.save(index_path)
    print(f"index: +{len(records)} filings -> {index_path} "
          f"(total {len(index.records)})")

    manifest = Manifest.load(manifest_path)
    for rec in records:
        plan = plan_from_discovery(rec)
        if manifest.get(plan.key()) is None:
            manifest.upsert(
                _filing_from_plan(plan, rec.period_to, rec.isin, rec.relating_to,
                                  rec.filing_ts)
            )
    manifest.save(manifest_path)
    print(f"manifest: {len(manifest)} records -> {manifest_path}")
    return 0


def _filing_from_plan(
    plan: FetchPlan, period_end: dt.date | None, isin: str | None,
    relating_to: str | None, filing_ts: dt.datetime | None,
) -> FilingRecord:
    return FilingRecord(
        symbol=plan.symbol,
        period_end=period_end,
        filing_id=plan.filing_id,
        isin=isin,
        relating_to=relating_to,
        filing_ts=filing_ts,
        xbrl_url=plan.xbrl_url,
        source=plan.source,
        consolidated=plan.consolidated,
        audited=plan.audited,
        cumulative=plan.cumulative,
        ind_as=plan.ind_as,
    )


def cmd_fetch(args: argparse.Namespace) -> int:
    index_path, manifest_path, payloads_dir = _paths(args.data_dir)
    index = DiscoveryIndex.load(index_path)
    if not index.records:
        print(
            f"no index at {index_path}; run `india-xbrl --index --source ...` first",
            file=sys.stderr,
        )
        return 2

    manifest = Manifest.load(manifest_path)
    plans = [plan_from_discovery(rec) for rec in index.records]
    if args.symbols:
        wanted = {s.upper() for s in args.symbols}
        plans = [p for p in plans if p.symbol in wanted]
    if args.limit is not None and args.limit >= 0:
        plans = plans[: args.limit]

    store = PayloadStore(payloads_dir)
    transport = RequestsTransport()

    def persist() -> None:
        manifest.save(manifest_path)

    fetcher = Fetcher(transport, store, manifest, workers=args.workers, persist=persist)
    try:
        fetcher.run(plans, skip_standalone_redundant=not args.no_skip_standalone)
    finally:
        manifest.save(manifest_path)
        transport.close()

    counts = manifest.status_counts()
    print("fetch complete:")
    print(f"  downloaded (ok): {counts[FetchStatus.OK]}")
    print(f"  no_xbrl_link (expected, not an error): {counts[FetchStatus.NO_XBRL_LINK]}")
    print(f"  skipped_standalone_redundant: {counts[FetchStatus.SKIPPED_STANDALONE_REDUNDANT]}")
    print(f"  failed: {counts[FetchStatus.FAILED]}")
    return 0 if counts[FetchStatus.FAILED] == 0 else 1


def cmd_audit(args: argparse.Namespace) -> int:
    _, manifest_path, payloads_dir = _paths(args.data_dir)
    manifest = Manifest.load(manifest_path)
    store = PayloadStore(payloads_dir)
    report = run_audit(manifest, store)
    print(report.summary())
    return 1 if report.has_integrity_issues() else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.audit:
        return cmd_audit(args)
    if args.index:
        return cmd_index(args)
    return cmd_fetch(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
