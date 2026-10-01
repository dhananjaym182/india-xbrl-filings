"""Audit: read-only coverage, checksum, and gap reporting. No network access.

``--audit`` is a first-class feature. It reports, offline:

- coverage by year and by symbol,
- checksum mismatches (stored file no longer matches the recorded SHA-256),
- files listed in the manifest but missing on disk (and orphans on disk),
- the full breakdown of ``fetch_status`` counts,
- taxonomy package status.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from india_xbrl.manifest import Manifest
from india_xbrl.models import FilingRecord, FetchStatus, TaxonomyFamily
from india_xbrl.store import PayloadStore
from india_xbrl.taxonomy import validate_packages


@dataclass
class AuditIssue:
    kind: str  # "missing_file" | "checksum_mismatch" | "unreadable_file"
    symbol: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.symbol}: {self.detail}"


@dataclass
class AuditReport:
    total_records: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    coverage_by_year: dict[int, int] = field(default_factory=dict)
    coverage_by_source: dict[str, int] = field(default_factory=dict)
    coverage_by_taxonomy: dict[str, int] = field(default_factory=dict)
    symbols: int = 0
    top_symbols: list[tuple[str, int]] = field(default_factory=list)
    issues: list[AuditIssue] = field(default_factory=list)
    orphans_on_disk: int = 0
    taxonomy_lines: list[str] = field(default_factory=list)

    @property
    def ok(self) -> int:
        return self.status_counts.get(FetchStatus.OK.value, 0)

    def has_integrity_issues(self) -> bool:
        return any(
            i.kind in {"missing_file", "checksum_mismatch", "unreadable_file"}
            for i in self.issues
        )

    def summary(self) -> str:
        lines: list[str] = []
        lines.append(f"manifest records: {self.total_records}")
        lines.append(f"symbols: {self.symbols}")
        lines.append("")
        lines.append("fetch_status counts:")
        for status in FetchStatus:
            lines.append(f"  {status.value:<32} {self.status_counts.get(status.value, 0)}")
        lines.append("")
        lines.append("coverage by period_end year:")
        if self.coverage_by_year:
            for year in sorted(self.coverage_by_year):
                lines.append(f"  {year}: {self.coverage_by_year[year]}")
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append("coverage by source endpoint:")
        for source in sorted(self.coverage_by_source):
            lines.append(f"  {source}: {self.coverage_by_source[source]}")
        lines.append("")
        lines.append("coverage by taxonomy family (recorded at download time):")
        for fam in sorted(self.coverage_by_taxonomy):
            lines.append(f"  {fam}: {self.coverage_by_taxonomy[fam]}")
        lines.append("")
        if self.top_symbols:
            lines.append("top symbols by filing count:")
            for sym, n in self.top_symbols[:10]:
                lines.append(f"  {sym}: {n}")
            lines.append("")
        if self.orphans_on_disk:
            lines.append(f"orphan payload files on disk (not in manifest): {self.orphans_on_disk}")
        lines.append("integrity:")
        if self.has_integrity_issues():
            for issue in self.issues:
                lines.append(f"  {issue}")
        else:
            lines.append("  all ok-recorded payloads present and hash-matching")
        lines.append("")
        lines.append("taxonomy packages:")
        for line in self.taxonomy_lines:
            lines.append(f"  {line}")
        return "\n".join(lines)


def run_audit(
    manifest: Manifest,
    store: PayloadStore,
    *,
    verify_checksums: bool = True,
    now: dt.date | None = None,
) -> AuditReport:
    """Audit the manifest + store. Performs no network access."""
    report = AuditReport(total_records=len(manifest))
    store_root = store.root.resolve()

    # Every status is reported, including zero counts.
    for status in FetchStatus:
        report.status_counts[status.value] = 0

    years: Counter[int] = Counter()
    sources: Counter[str] = Counter()
    taxonomies: Counter[str] = Counter()
    symbols: Counter[str] = Counter()
    seen_paths: set[Path] = set()

    for rec in manifest.records:
        report.status_counts[rec.fetch_status.value] = (
            report.status_counts.get(rec.fetch_status.value, 0) + 1
        )
        sources[rec.source.value] += 1
        taxonomies[rec.taxonomy_family.value] += 1
        if rec.symbol:
            symbols[rec.symbol] += 1
        if rec.period_end is not None:
            years[rec.period_end.year] += 1

        if rec.fetch_status != FetchStatus.OK:
            continue
        if not rec.raw_path:
            report.issues.append(
                AuditIssue("missing_file", rec.symbol, "ok record has no raw_path")
            )
            continue
        path = Path(rec.raw_path)
        seen_paths.add(path.resolve())
        if not path.is_file():
            report.issues.append(
                AuditIssue("missing_file", rec.symbol, f"listed but absent: {path}")
            )
            continue
        if not verify_checksums:
            continue
        try:
            raw = store.read_raw(path)
        except (OSError, EOFError) as exc:
            report.issues.append(
                AuditIssue("unreadable_file", rec.symbol, f"{path}: {exc}")
            )
            continue
        from india_xbrl.store import sha256_bytes

        if sha256_bytes(raw) != (rec.raw_sha256 or ""):
            report.issues.append(
                AuditIssue(
                    "checksum_mismatch",
                    rec.symbol,
                    f"{path}: manifest {rec.raw_sha256} != actual "
                    f"{sha256_bytes(raw)}",
                )
            )

    report.coverage_by_year = dict(sorted(years.items()))
    report.coverage_by_source = dict(sources)
    report.coverage_by_taxonomy = dict(taxonomies)
    report.symbols = len(symbols)
    report.top_symbols = symbols.most_common(10)

    # Orphans: payload files on disk that the manifest does not reference.
    for path in store_root.rglob("*.xml.gz"):
        if path.resolve() not in seen_paths:
            report.orphans_on_disk += 1

    report.taxonomy_lines = validate_packages()
    return report
