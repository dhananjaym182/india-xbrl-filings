"""CLI tests: --audit runs offline with exit codes; argument surface."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from india_xbrl import cli
from india_xbrl.manifest import Manifest
from india_xbrl.models import FilingRecord, FetchStatus, Source
from india_xbrl.store import PayloadStore


def _seed(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    store = PayloadStore(data_dir / "payloads")
    manifest = Manifest()
    path, digest, nbytes = store.write(b"<xbrl/>", "TCS", "1", "https://x/tcs.xml")
    manifest.upsert(
        FilingRecord(
            symbol="TCS",
            period_end=dt.date(2021, 3, 31),
            filing_id="1",
            xbrl_url="https://x/tcs.xml",
            source=Source.NSE_LEGACY,
            fetch_status=FetchStatus.OK,
            raw_path=str(path),
            raw_sha256=digest,
            raw_bytes=nbytes,
        )
    )
    manifest.save(data_dir / "manifest.jsonl")


def test_audit_command_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(tmp_path / "data")
    rc = cli.main(["--audit", "--data-dir", str(tmp_path / "data")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "manifest records" in out
    assert "fetch_status" in out


def test_audit_command_detects_corruption(tmp_path: Path) -> None:
    _seed(tmp_path / "data")
    import gzip

    payload_path = next((tmp_path / "data" / "payloads").rglob("*.xml.gz"))
    with gzip.open(payload_path, "rb") as gz:
        raw = gz.read()
    with open(payload_path, "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:
            gz.write(raw + b"-tampered")

    rc = cli.main(["--audit", "--data-dir", str(tmp_path / "data")])
    assert rc == 1  # integrity issue -> nonzero exit


def test_audit_empty_data_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["--audit", "--data-dir", str(tmp_path / "missing")])
    assert rc == 0  # nothing on disk yet: clean but empty audit
    out = capsys.readouterr().out
    assert "manifest records: 0" in out


def test_fetch_requires_index(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["--fetch", "--data-dir", str(tmp_path / "data")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--index" in err


def test_years_parse_error_message(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli._parse_years("not-a-year")


def test_mutually_exclusive_modes() -> None:
    with pytest.raises(SystemExit):
        cli.main(["--index", "--audit"])


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    from india_xbrl import __version__

    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out
