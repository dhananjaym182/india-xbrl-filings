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

BSE's RSS feed is a fixed "latest" snapshot and suits this incremental use.

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

## BSE works — the "blocked" claim was a fingerprint artifact

BSE is supported (`--source bse`). Measured 2026-10-01 against the live endpoints.

**The gate is a browser-fingerprint rule, not a ban.** `api.bseindia.com` sits behind
Akamai, which scores request headers:

- naive `python-requests` default User-Agent → **403 on every path**;
- a bare Chrome User-Agent → 403 on *some* requests and not others — the trap that
  fooled earlier audits into "BSE is blocked";
- the full set in `india_xbrl.transport.BSE_HEADERS` — Chrome UA **plus**
  `Accept-Encoding` (requests always sends it; `curl` does not: an A/B
  `curl --compressed` flips 403 → 200 on an identical URL) **plus** the `sec-ch-ua`
  client hints, Origin/Referer and Sec-Fetch-* → **HTTP 200, reliably**.

One transient 403 was observed right after a large PDF download, and a 30 s
`ReadTimeout` mid-backfill — so **discovery GETs retry with backoff** too
(`BseDiscoveryClient`), and fail loudly once exhausted.

Full-year live result (calendar 2026): **13,946 filings discovered** (13,937
announcement rows back across the whole year + deduped RSS items) in ~5 minutes,
including result announcements back to 2016 for long-dead symbols.

### BSE discovery endpoints

Financial-result announcements — found in BSE's own SPA URL map
(`www.bseindia.com/assets/includenew/js/chunk-*.js`):

    https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w
        ?pageno=N &strCat=Result &strPrevDate=YYYYMMDD &strScrip=
        &strSearch=P &strToDate=YYYYMMDD &strType=C &subcategory=-1

- `strCat=Result` is the category **name**; the numeric id (`7`) returns 0 rows.
- Response: `{"Table": [rows], "Table1": [{"ROWCNT": total}]}`; ~50 rows/page; the
  client pages until `ROWCNT` is covered, slicing the range into ≤7-day windows.
- Windows verified **back to January 2019** — full history, no NSE-style cutoff.
- Rows carry an `ATTACHMENTNAME` (result PDF from
  `www.bseindia.com/xml-data/corpfiling/AttachLive/`) but **no XBRL instance link**.
  Rows without an attachment index as `no_xbrl_link` — the same recorded outcome as
  NSE. (Legacy `XBRLFILES/CGXBRLDataXML/ANN_*.xml` paths are dead: 404 for 2019 rows.)
- **`AttachLive` rotates: roughly the last 60 days of attachments survive**
  (measured 2026-10-01: a Sep 24 attachment → 200 at 2.9 MB; Aug 7 and older → 404,
  including under `AttachHistoric`). Older filings are *discoverable* but their PDFs
  are gone from BSE entirely. Record outcomes and move on — the fetcher does.
- Subjects are machine-parsed for the period and carry **year typos** (live
  examples: "Quarter Ended June 30, 3036", "Dec 31, 20925"). Implausible dates are
  refused (`period_end=None`), never silently "corrected".

Integrated-Filing (IFIndAs) instances come from a fixed "latest" RSS snapshot —
useful for incremental sync, not backfill:

    https://www.bseindia.com/Data/XML/FinancialResultsFeed.aspx   (no params)

Each item links into `www.bseindia.com/XBRLFILES/IFIndasDuplicateUploadDocument/` —
and here the ixbrl trap is **inverted** (see below): the `.html` is real iXBRL while
the **`.xml` sibling is the plain instance** our reader accepts. The library maps
`.html` → `.xml` for the payload and keeps the original as `ixbrl_url` provenance.
The feed **repeats exact rows** (observed live); duplicates are dropped on the
(symbol, instance) pair, while a changed instance for the same symbol is kept as a
distinct filing.

Verified end-to-end 2026-10-01: an RSS `.xml` instance downloaded through the same
fetcher parses with the vendored `in-capmkt` reader — 259 facts, 11 contexts,
**zero dropped namespaces** (`IFIndAs V2.1`, namespace dated `2026-01-31`).

Verified dead ends, so you don't re-walk them: `XbrlAnnouncementCategory/w` serves
Reg-30 announcements only; `strCat=8` (Integrated Filing id) returns 0 rows; the
legacy result pages (`Corp_FinanceResult_ng_new`, `Integratedfinancedata`,
`Corp_Archive_Wthxml_ng(_new)`, `Result_Arch_ng`) 302 to an error page when called
with guessed parameters; `m.bseindia.com` serves HTML but its API paths 404.

## The two taxonomies — and the 2025 cutoff

| | legacy "Financial Results" | new "Integrated Filing" |
|---|---|---|
| endpoint | `corporates-financial-results` | `integrated-filing-results` (paginated) |
| taxonomy | **`in-bse-fin`** (BSE-published) | **`in-capmkt`** (SEBI-published) |
| facts / filing | ~112 | **~931** (~144 contexts) |
| coverage | FY2015-16 onward | **2025+ ONLY** |

**Row shapes (measured live 2026-10-01, both endpoints):** the two endpoints use
different field names — legacy rows carry `seqNumber`/`toDate`/`filingDate`,
integrated rows carry `seq_Id`/`qe_Date`/`creation_Date` — and NSE's null sentinel
`-` leaks through even **pre-joined** into `.../xbrl/-` URLs (which 404). Classifications
are *words*, not booleans: `"Audited"`/`"Un-Audited"`, `"Consolidated"`/`"Non-Consolidated"`,
`"Ind-AS New"`. A boolean-only mapper silently records every row as unaudited; the
mapper here handles both shapes and both sentinel forms.

The new taxonomy is **not backwards compatible**. A parser that expects `in-capmkt`
across history gets **zero matches before 2025** — 32/32 sampled old-format filings used
`in-bse-fin`, 0/32 used `in-capmkt`. The `in-bse-fin` prefix **contains hyphens**; a
naive `[A-Za-z0-9_]+` regex silently matches nothing, so prefix patterns here allow `-`.

Both are plain XBRL 2.1 (not iXBRL). `in-ind-as` is claimed by a third-party library but
was seen in **no** actual file — treated as unconfirmed, not built upon.

### The `ixbrl` trap

Integrated Filing records also carry an `ixbrl` field. It points at an `.html` that
contains **zero `ix:` tags and zero XBRL facts** — a rendered table for humans. This
library reads the `xbrl` field and never the `ixbrl` field (enforced by test), because
downloading it wastes bandwidth and silently produces an empty parse.

**BSE inverts the trap.** In BSE's Integrated Filing RSS, the `.html` *is* iXBRL
(redirected facts and all) and the `.xml` sibling is the plain instance — both real.
We fetch the `.xml` sibling and keep the `.html` link as provenance.

## Parsing

**Taxonomy resolution is broken upstream.** The `schemaRef` in each instance is
*relative* (e.g. `in-capmkt-ent-2026-01-31.xsd`) and **neither host serves the .xsd**:
`nsearchives.nseindia.com/corporate/xbrl/*.xsd` → 404; `sebi.gov.in/xbrl/...` → 530. A
strict DTS-resolving parser (plain Arelle) **cannot** resolve the taxonomy from the
instance alone.

The design decision, implemented in `india_xbrl/taxonomy.py` and `india_xbrl/reader.py`:

1. **Primary — side-load the official SEBI taxonomy package.** The SEBI "Integrated
   Filing Finance (IndAS)" package is vendored at `vendor/taxonomies/in-capmkt-finance/`
   (a valid XBRL Taxonomy Package: `META-INF/taxonomyPackage.xml`, `catalog.xml` with a
   `rewriteURI` rule, entry point `in-capmkt-ent-2025-01-31.xsd`, publisher SEBI, plus
   lab/cal/def/pre/ref linkbases and a tag→label XLSX), versioned by its publication date
   `2025-01-31`. Siblings exist upstream for NBFC, Banking, General Insurance, Life
   Insurance, and "Other than banks". This is the correct long-term path.
2. **Fallback — namespace-agnostic reading.** Strip prefixes, match local names, keep
   `OneD` (discrete quarter) and `FourD` (cumulative YTD) contexts **distinct** — both
   appear in the same filing and conflating them silently doubles or halves every flow
   value. Anything dropped (unparseable namespace prefixes, no-namespace facts) is
   recorded in `InstanceSummary.dropped_namespaces`, never silently discarded.

Point Arelle at the vendored package directory when you do adopt it (v1 ships no Arelle
dependency); `TaxonomyRegistry` already speaks its catalog conventions.

## Rate limits (measured, not guessed)

Measured against `nsearchives.nseindia.com` (2026-10-01):

- **0.6 s spacing → 8 of 20 fetches FAILED.**
- **6 s spacing × escalating per-attempt backoff → every retry succeeded.**
- Throughput observed: **~0.31 filings/s at 12 concurrent workers.** NSE holds
  connections ~30 s, so throughput scales with **concurrency, not with a shorter delay**.

Default: **12 workers**, 6 s per-worker spacing, backoff `base × (attempt + 1)` plus
random jitter (without the jitter, concurrent workers retry in lockstep and re-trigger
the rate limit together). **Raising concurrency is a politeness decision, not a
performance one.** Forcing HTTP/1.1 to the archives host is required — HTTP/2 fails with
`INTERNAL_ERROR` — and is pinned in `india_xbrl/transport.py`.

## `no_xbrl_link` is expected

A large fraction of companies genuinely have no XBRL for a period: in the reference
corpus **~33%** of discovery rows (`9,487 of 28,841`) had no XBRL link — small and
pre-Ind-AS filers. This is **not an error**; it is the correct answer for a company that
does not file XBRL. It is recorded as its own `fetch_status` and never retried.

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

## What we deliberately do not do

- No cookie/session layer: NSE's discovery APIs return HTTP 200 with only a browser
  User-Agent, and BSE needs the full header fingerprint (see above) but no cookies on
  either host. A cookie layer is unnecessary complexity that rots.
- No HTML scraping, no browser automation: the JSON APIs are stable, the HTML is
  client-rendered.
- No abandoned dependencies (`nsepy`, `investpy`, `python-xbrl`, `xbrl-parser`, …).
- No silent namespace-dropping: whatever the reader drops is recorded on the summary.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e . pytest pytest-cov mypy types-requests
pytest                # offline; mocked transport only
mypy                  # strict
```

### Publishing to PyPI

CI handles it via [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
(OIDC — no tokens anywhere):

1. **One-time**: on pypi.org → Account settings → Publishing, register a pending
   publisher: project `india-xbrl-filings`, owner `dhananjaym182`, repo
   `india-xbrl-filings`, workflow `release.yml`, environment `pypi`.
2. Bump `version` in `pyproject.toml`, commit, then:

   ```bash
   git tag v0.1.0 && git push origin main v0.1.0
   ```

   The `release` workflow builds, `twine check`s, smoke-tests the wheel in a
   clean venv, and publishes. Artifacts are also uploaded as workflow artifacts.

Locally, the same artifacts are reproducible with `python -m build` +
`twine check dist/*`.

Acceptance coverage (all offline): resume across interrupt; one-byte corruption caught
by `--audit`; both fixture taxonomies parse with `OneD`/`FourD` distinct; `ixbrl` never
fetched; `no_xbrl_link` never retried; pacing/backoff asserted via fake clock; full
index+fetch path runs against a mocked transport.

## License

MIT — see [LICENSE](LICENSE).
