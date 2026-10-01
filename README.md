# india-xbrl-filings

Discover and download **Indian listed-company financial-result XBRL filings** (NSE and BSE) with full provenance.

> [!IMPORTANT]
> **Personal/educational use only — not for commercial use.** Unofficial endpoints,
> not affiliated with NSE/BSE/SEBI, no warranty. See [Legal note](#legal-note--read-this).

**Scope:** download + preserve + index only. Parsing XBRL into normalized financial
statements is a non-goal for v1 (see [Parsing](#parsing) below).

- Raw payloads preserved **byte-exact, gzipped, with SHA-256 recorded** — normalization
  must be rebuildable from disk without re-fetching.
- **The manifest IS the checkpoint**: an append-only JSONL file, rewritten atomically.
  Resume = skip `ok` records whose file still exists AND still hash-matches. No database.
- **Revisions preserved, never collapsed.** Multiple filings for the same
  `(symbol, period_end, basis)` are the norm (consolidated vs standalone, re-filings);
  dedup is a downstream decision recorded there, never a silent deletion here.
- Every outcome is **recorded, not thrown**: `ok`, `no_xbrl_link`,
  `skipped_standalone_redundant`, `pending`, `failed`.

## How to use

### 1. Install

From PyPI:

```bash
pip install india-xbrl-filings
india-xbrl --version
```

From source:

```bash
git clone https://github.com/dhananjaym182/india-xbrl-filings
cd india-xbrl-filings
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
india-xbrl --version
```

Python 3.10+ required; the only runtime dependency is `requests`.

### 2. Index (discover filings)

```bash
india-xbrl --index --source nse --years 2015-2026   # NSE (default source)
india-xbrl --source bse --index --years 2019-2026   # BSE
india-xbrl --source all --index --years 2019-2026   # both, one index
```

This writes `data/index.jsonl` (the discovery fingerprint) and seeds
`data/manifest.jsonl` (the checkpoint). Discovery is **append-safe**: each run
extends the index, tagging records with the source that produced them, so mixed
NSE+BSE corpora are safe.

Expect roughly: NSE ~4k filings/year (2026), BSE ~14k filings/year (2026),
~5 minutes per BSE year. Nothing here needs an API key or login.

### 3. Fetch (download payloads, resumable)

```bash
india-xbrl --fetch                      # everything, resumable
india-xbrl --fetch --limit 50           # small test run first — start here
india-xbrl --fetch --symbols TCS RELIANCE   # specific NSE symbols
india-xbrl --fetch --symbols BSE-500013 BSE-532614  # specific BSE scrips
india-xbrl --fetch --workers 4          # be more polite than the default
```

`--fetch` resumes from the manifest: interrupt it at any point, re-run, and
already-`ok` records are skipped after existence + SHA-256 verification.
Payloads land gzipped under `data/payloads/`, byte-exact, checksums recorded.

Two expected, not-an-error outcomes you will see:

- `no_xbrl_link` — the company/period genuinely has no XBRL (see below);
- `failed` on old BSE rows — BSE keeps attachment PDFs only ~60 days.

### 4. Audit (offline integrity report)

```bash
india-xbrl --audit
```

Performs **no network access** and reports:

- coverage by year, by symbol, by source endpoint, by taxonomy family,
- the full breakdown of `fetch_status` counts,
- checksum mismatches and files listed in the manifest but missing on disk,
- orphan payload files on disk,
- taxonomy package status.

Exit code `1` on any integrity issue — CI-friendly.

### 5. Keep it fresh (incremental sync)

```bash
# e.g. a weekly cron; re-index the recent window and fetch the new arrivals
india-xbrl --source all --index --years 2026 && india-xbrl --fetch
```

### All options

| flag | meaning | default |
|---|---|---|
| `--source nse\|bse\|all` | exchange(s) to discover from | `nse` |
| `--years A-B` | calendar (`2015-2026`) or Indian fiscal (`2015-16`) | `2015-2026` |
| `--limit N` | fetch at most N filings this run | unlimited |
| `--symbols SYM...` | restrict fetch to these symbols/scrips | all |
| `--workers N` | concurrent downloads (politeness knob) | 12 |
| `--no-skip-standalone` | also fetch standalone when a consolidated filing exists for the same symbol+period | off |
| `--data-dir DIR` | where `index.jsonl` / `manifest.jsonl` / `payloads/` live | `data/` |
| `-v` | debug logging (per-request lines) | off |

## Legal note — read this

> **Personal / educational use only.**
>
> - **NOT for commercial use.** Do not use this tool as a data source for any
>   commercial product, service, or resale — get a licensed data feed instead.
> - **NOT affiliated with or endorsed by NSE, BSE, or SEBI** in any way.
> - The exchanges expose **unofficial, undocumented endpoints**. Everything here
>   can break without notice, and download behavior may violate the exchanges'
>   terms of use — **you use this tool entirely at your own risk**.
> - Respect the rate limits built into the tool (and be more conservative than
>   the defaults if you share the hosts with other users).
> - The authors accept **no liability** for any damages, data loss, account
>   restrictions, or legal exposure arising from use of this software. The
>   MIT license's disclaimer of warranty applies in full.
>
> If you need production-grade market data, license it from NSE, BSE, or an
> authorized vendor. Nothing here is investment advice.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e . pytest pytest-cov mypy types-requests
pytest                # offline; mocked transport only
mypy                  # strict
```

Acceptance coverage (all offline): resume across interrupt; one-byte corruption caught
by `--audit`; both fixture taxonomies parse with `OneD`/`FourD` distinct; `ixbrl` never
fetched; `no_xbrl_link` never retried; pacing/backoff asserted via fake clock; full
index+fetch path runs against a mocked transport.

## License

MIT — see [LICENSE](LICENSE).
