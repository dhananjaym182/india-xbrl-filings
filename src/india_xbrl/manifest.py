"""The manifest is the checkpoint.

Design commitments (these made the reference fetcher work; keep all of them):

- **Append-only JSONL, rewritten atomically each cycle.** Resume = skip any
  record already marked ``ok`` whose local file still exists AND still
  hash-matches. Killing the process loses at most the in-flight filing.
- **No database.** A file is inspectable, diffable, and survives a schema
  change; a binary store does not.
- **Revisions are preserved, never collapsed.** Multiple filings for the same
  ``(symbol, period_end, basis)`` are the norm (measured: 11,977 of 15,561
  symbol-period pairs have >1 filing -- consolidated vs standalone plus
  genuine re-filings). Deduplication is a downstream decision recorded
  there, never a silent deletion here.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

from india_xbrl.jsonio import discovery_from_json, discovery_to_json, filing_from_json, filing_to_json
from india_xbrl.models import DiscoveryRecord, FilingRecord, FetchStatus


class Manifest:
    """In-memory view of the manifest with atomic save."""

    def __init__(self, records: list[FilingRecord] | None = None) -> None:
        self._records: list[FilingRecord] = records if records is not None else []
        self._index: dict[tuple[str, str], FilingRecord] = {}
        self._pos: dict[tuple[str, str], int] = {}
        for i, rec in enumerate(self._records):
            self._index[rec.key()] = rec
            self._pos[rec.key()] = i

    # ------------------------------------------------------------------ #

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[FilingRecord]:
        return iter(self._records)

    @property
    def records(self) -> list[FilingRecord]:
        return list(self._records)

    def get(self, key: tuple[str, str]) -> FilingRecord | None:
        return self._index.get(key)

    def upsert(self, record: FilingRecord) -> bool:
        """Insert or replace by key. Returns True if newly inserted.

        Never silently drops a record: an updated row replaces the old one
        under the same key, preserving revision multiplicity across distinct
        keys (filing_id / xbrl_url).
        """
        key = record.key()
        pos = self._pos.get(key)
        if pos is None:
            self._pos[key] = len(self._records)
            self._records.append(record)
            self._index[key] = record
            return True
        self._records[pos] = record
        self._index[key] = record
        return False

    # ------------------------------------------------------------------ #

    def save(self, path: str | os.PathLike[str]) -> None:
        """Rewrite the manifest atomically: tmp file + os.replace."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for rec in self._records:
                    fh.write(_dumps_line(filing_to_json(rec)))
                    fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Manifest:
        path = Path(path)
        if not path.exists():
            return cls()
        records: list[FilingRecord] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                import json

                doc = json.loads(line)
                records.append(filing_from_json(doc))
        return cls(records)

    # ------------------------------------------------------------------ #

    def status_counts(self) -> dict[FetchStatus, int]:
        counts: dict[FetchStatus, int] = {s: 0 for s in FetchStatus}
        for rec in self._records:
            counts[rec.fetch_status] += 1
        return counts

    def ok_records(self) -> list[FilingRecord]:
        return [r for r in self._records if r.fetch_status == FetchStatus.OK]


class DiscoveryIndex:
    """On-disk JSONL index of discovery rows (fingerprint)."""

    def __init__(self, records: list[DiscoveryRecord] | None = None) -> None:
        self.records: list[DiscoveryRecord] = records if records is not None else []

    def save(self, path: str | os.PathLike[str]) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for rec in self.records:
                    fh.write(_dumps_line(discovery_to_json(rec)))
                    fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> DiscoveryIndex:
        path = Path(path)
        if not path.exists():
            return cls()
        records: list[DiscoveryRecord] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                import json

                doc = json.loads(line)
                records.append(discovery_from_json(doc))
        return cls(records)


def _dumps_line(doc: dict[str, object]) -> str:
    import json

    return json.dumps(doc, ensure_ascii=False, sort_keys=False)
