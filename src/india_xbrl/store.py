"""Byte-exact payload storage.

Every raw payload is preserved **byte-exact, gzipped, with its SHA-256
recorded**. Never store a parsed form as the only artifact: normalization must
be rebuildable from disk without re-fetching. Taxonomy mappings improve over
time; re-downloading 163,000 filings to apply a mapping fix is not acceptable.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PayloadStore:
    """Stores filing payloads gzipped under a content-derived path."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def path_for(self, symbol: str, filing_id: str | None, xbrl_url: str | None) -> Path:
        """Deterministic path: <root>/<SYMBOL>/<symbol>_<key>.xml.gz."""
        ident = filing_id or _url_hash(xbrl_url or "") or "unknown"
        safe_symbol = _safe(symbol)
        name = f"{safe_symbol}_{_safe(ident)}.xml.gz"
        return self.root / safe_symbol / name

    def write(
        self, payload: bytes, symbol: str, filing_id: str | None, xbrl_url: str | None
    ) -> tuple[Path, str, int]:
        """Write payload gzipped; returns (path, sha256 of *raw* bytes, raw size).

        The SHA-256 is always over the **raw (uncompressed)** payload so the
        manifest checksum can be verified against the source of truth.
        """
        path = self.path_for(symbol, filing_id, xbrl_url)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = sha256_bytes(payload)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("wb") as fh:
            with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:
                gz.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return path, digest, len(payload)

    def read_raw(self, path: str | os.PathLike[str]) -> bytes:
        """Read a stored payload and return the raw (decompressed) bytes."""
        with gzip.open(path, "rb") as gz:
            return gz.read()

    def verify(self, path: str | os.PathLike[str], expected_sha256: str) -> bool:
        """True if the stored payload's raw SHA-256 matches the manifest."""
        try:
            return sha256_bytes(self.read_raw(path)) == expected_sha256
        except (OSError, gzip.BadGzipFile, EOFError):
            return False


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)[:120]


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
