# Market Radar — Build Spec

Personal research tool. Public equities intelligence + private company deal
sourcing. Information gathering only.

Running cost: **$30/month** (Tiingo Power). Everything else is free tier or
public bulk data.

Companion doc: `CLAUDE.md` at repo root covers conventions, hard rules, and
gotchas. This doc covers what to build and in what order.

---

## 0. Pre-flight

Do these before writing code. Each is ~15 minutes and any of them can change
your source choices.

- [x] **Stooq bulk automation.** *Resolved 2026-09-04: not automatable.*
      Stooq put the bulk archive behind a CAPTCHA on 2020-12-10. The
      per-ticker CSV endpoint (`stooq.com/q/d/l/`) has a low undocumented
      daily quota and returns `Exceeded the daily hits limit`. Stooq is
      therefore **not** the whole-market source of record. See §2.
- [x] **Tiingo pricing.** *Resolved 2026-09-04:* Power is **$30/month**, not
      $10. The $10 figure in earlier drafts was years stale. Published Power
      limits: 10,000 req/hour, 100,000 req/day, ~109,865 unique symbols/month,
      40 GB/month bandwidth. Rate limits reset hourly; bandwidth on the 1st.
- [x] **Tiingo news access.** *Resolved 2026-09-04:* Tiingo News is included
      on all plans, **not** a paid add-on. Constraint is depth, not access —
      3 months of queryable history plus everything going forward.
- [x] **OTC coverage — Tiingo has it.** *Resolved 2026-09-07 from the
      supported-ticker file.* Tiingo carries **17,618 active OTC symbols**:
      PINK 16,301, OTCMKTS 857, OTCQB 211, OTCGREY 190, OTCD 31, OTCQX 15,
      OTCCE 10, OTCBB 3. Stooq's US bulk remains listed-only, so Tiingo is the
      only viable source for the sub-$1 OTC band.

      **Decision (2026-09-07): hold OTC off until the listed sweep has run
      clean for one week, then turn it on.** Deferred deliberately, not
      forgotten. Cost of enabling: the nightly universe goes from ~14,100 to
      ~31,700 symbols and the sweep from **~95 min to ~211 min** at 9,000
      req/hour — still inside the 240-minute job timeout, but only just, so
      the timeout should be raised at the same time. Turning it on is one
      line: add `OTC_EXCHANGES` into `LISTED_EXCHANGES` in
      `sources/tiingo.py`. Revisit once seven consecutive nightly runs are
      green.

      Still worth doing with the ten tickers: confirm *history quality* on
      names actually traded, which a symbol count does not tell us.
- [x] **NYSE American is not being dropped.** *Checked 2026-09-07.* Tiingo
      splits the venue across two codes — AMEX (298) and NYSE MKT (34) — and
      both are kept, so 332 active listings are in scope. The low "NYSE MKT"
      figure is a labelling artefact. Eight NYSE `ATEST*` exchange test
      symbols were being included and are now filtered out.
- [ ] **Rotate the SAM.gov API key.** *Deferred 2026-09-06, deliberately.*
      It was exposed in a tool transcript by an early version of
      `scripts/scan_secrets.py`, which classified secrets by variable name
      and printed anything it judged non-secret. The key is read-only access
      to public federal contract data and is not used until Weekend 4, so the
      rotation was deferred rather than skipped. **Rotate before
      `sources/usaspending.py` is written.** Cloudflare R2 and Finnhub keys
      exposed by the same bug were rotated on the day.
- [ ] **State bulk files.** For your target states, check whether the
      Secretary of State publishes or sells bulk entity data, and whether UCC
      filings are available in bulk. Only write a scraper where no bulk option
      exists. **Still open — Weekend 4, does not block Weekends 1–3.**
- [ ] **Free tier limits.** Re-check current caps for Google AI Studio, Groq,
      Cerebras, GitHub Models, Supabase, and GitHub Actions. These move.
      Note: Supabase free projects pause after ~7 days of inactivity; the
      nightly job keeps it warm, but a long gap will require a manual resume.

---

## 1. Architecture

Three tiers. Each narrows the funnel before expensive work runs.

**Tier 0 — whole market, nightly, ~free.** Bulk EOD prices for all US tickers.
Volatility screens are SQL window functions over local Parquet. Zero per-ticker
API calls.

**Tier 1 — sentinels.** EDGAR RSS filtered by form type, GDELT news, bulk
private-company datasets. Deterministic filters, almost no LLM.

**Tier 2 — deep dive.** Only names promoted by Tier 0, Tier 1, or a manual
click. The only tier allowed to be expensive.

Tier 2 has **two sub-levels**, which earlier drafts collapsed into one number
and thereby oversized the LLM budget:

- **Processed and summarized — 15–40/day.** XBRL pull, comps, forward-return
  lookup, one-paragraph summary. Output is a digest line. Cheap-tier LLM.
- **Full deck — 3–5/day.** DCF, narrative analysis, 10-page python-pptx
  output. Good-tier LLM. This is the number to budget quota against, and the
  number bounded by what one person can actually read in a day.

Promotion from the first to the second is a deliberate act — a screen result
you clicked, not an automatic cascade.

### Storage

| Layer | Holds | Limit |
|---|---|---|
| Supabase free | entities, signals, deals, queue, manifest stats | 500 MB |
| Cloudflare R2 | **vendor-derived** Parquet (prices, corporate actions) | 10 GB free |
| GitHub Releases | **public-domain** Parquet (SEC, DOL, FRED, USAspending) | 2 GB / asset |

**The split between R2 and Releases is a licensing boundary, not a capacity
one.** This repo is public, and GitHub Release assets on a public repo are
publicly downloadable — that is redistribution. Tiingo and Stooq free/personal
terms do not permit it. Government data is public domain and fine to
republish. The manifest's `backend` column carries this distinction; anything
a vendor touched goes to R2 (private bucket, credentialed reads).

- Partition Parquet by year. Because storage is **raw** OHLCV (see §4),
  history is immutable and nightly writes genuinely only touch the current
  year. Adjustment happens at query time, so a new split rewrites nothing.
- DuckDB is the query engine. It reads Parquet over HTTP with range requests
  and can attach Postgres in the same session, so cross-store joins are one
  SQL statement.
- Cache downloads in Actions keyed on `dataset_stats.updated_at`.

### The manifest

Two things that were conflated in earlier drafts, now split:

**Location is configuration** — changes rarely, deliberately, by a human.
Lives in `manifest.toml` in git, where it is diffable and revertable. Env var
`MR_MANIFEST_OVERRIDE` points at a local file so tests and local dev never
touch the network.

**Freshness is observation** — changes nightly, written by loaders, read by
the freshness assertions. Lives in Postgres, appended not overwritten, so the
history is queryable ("when did prices last actually grow?").

```toml
# manifest.toml
[prices_eod_raw.2026]
location = "s3://market-radar/prices_eod_raw/2026.parquet"
backend  = "r2"

[sec_companyfacts.all]
location = "https://github.com/<owner>/market-radar/releases/download/sec-2026/companyfacts.parquet"
backend  = "github_release"
```

```sql
create table dataset_stats (
  id           bigserial primary key,
  dataset      text not null,
  partition    text not null,
  row_count    bigint not null,
  max_date     date,
  observed_at  timestamptz not null default now()
);
create index on dataset_stats (dataset, partition, observed_at desc);
```

`manifest.get(dataset, partition)` stays the only public API regardless of
backing store. Nothing in the codebase hardcodes a URL.

### Core schema

```sql
create table companies (
  id            bigserial primary key,
  cik           text unique,
  ticker        text,
  name          text not null,
  normalized    text not null,       -- for fuzzy matching
  sic           text,
  naics         text,
  ein           text,
  is_public     boolean default false
);
create index on companies (normalized);

create table signals (
  id            bigserial primary key,
  company_id    bigint references companies(id),
  kind          text not null,       -- 'form4' | 'deal_filing' | 'news' | 'vol_screen' | 'form5500'
  source        text not null,
  occurred_at   timestamptz not null,
  payload       jsonb not null,
  url           text,
  -- idempotency: re-running a poll must not duplicate
  unique (kind, source, company_id, occurred_at)
);
create index on signals (company_id, occurred_at desc);
create index on signals (kind, occurred_at desc);

create table job_queue (
  id            bigserial primary key,
  task          text not null,
  args          jsonb not null,
  priority      int default 100,     -- lower runs first
  status        text default 'pending',
  attempts      int default 0,
  locked_at     timestamptz,         -- set on claim; stale locks are reclaimable
  last_error    text,
  created_at    timestamptz default now()
);
create index on job_queue (status, priority, created_at);
-- one pending copy of a given task+args at a time
create unique index on job_queue (task, args) where status = 'pending';

-- Raw prices are stored unadjusted in Parquet; adjustment is a query-time
-- join against this table. See §4.
create table corporate_actions (
  id            bigserial primary key,
  ticker        text not null,
  ex_date       date not null,
  split_factor  numeric(18,8) not null default 1,   -- Tiingo splitFactor
  div_cash      numeric(18,8) not null default 0,   -- Tiingo divCash
  source        text not null,
  ingested_at   timestamptz not null default now(),
  unique (ticker, ex_date, source)
);
create index on corporate_actions (ticker, ex_date);
```

No embeddings table yet. `halfvec` and the 256-dim decision are deferred until
something actually needs vector search — per §2, similarity is a ranking
refinement inside an already-filtered result set, so the row count will be
small and the storage optimization is premature.

---

## 2. Data sources

### Public markets

| Source | Gives | Access | Cost |
|---|---|---|---|
| **Tiingo Power** | **Source of record.** Raw + adjusted OHLCV, `splitFactor`, `divCash`, news | REST, all symbols, 100k/day | $30/mo |
| Stooq | Parser test fixture + independent cross-check | Manual bulk zip (CAPTCHA) | Free |
| **yfinance** | Tier 2 fundamentals enrichment — mkt cap, EV, P/E, EV/EBITDA, shares out | **Local only**, 15–40 req/day | Free |
| Finnhub | Quote cross-check, ticker-tagged news | REST, 60/min | Free |
| SEC `companyfacts.zip` | Full XBRL financials, all filers | Nightly bulk zip | Free |
| SEC Financial Statement Data Sets | Quarterly structured statements | Bulk zip | Free |
| SEC `company_tickers.json` | CIK ↔ ticker map | Single file | Free |
| FRED | 10-year Treasury, ICE BofA credit spreads | REST, 120/min | Free |
| GDELT | Wide-net news, 15-min refresh | Document API | Free |
| SEC trading suspensions | Halted micro caps | Published list | Free |

### Tier 0 price pipeline

**Tiingo is the source of record for both the history seed and the nightly
increment.** The daily endpoint accepts a date range, so one pass of ~12k
requests — one per ticker — retrieves 10 years of history for the whole
market. The same loop with a one-day range is the nightly job. Published
limits (100k req/day, 10k req/hour) accommodate both with room to spare.

**Requests are chunked and resumable.** A 12k-request sweep takes over an
hour against the hourly cap and *will* be interrupted — by a rate-limit
pause, a network blip, or an Actions timeout. A failure at request 9,000 must
not discard the first 9,000. Requirements:

- Fixed-size chunks over a deterministically ordered ticker list, so chunk
  boundaries are reproducible across runs.
- A checkpoint written after each chunk completes, recording which chunks are
  done for that logical run.
- Resume is the default on re-invocation: skip completed chunks, retry the
  rest. `--restart` is an explicit opt-in flag, never the default.
- Chunk results land as separate files, merged into the year partition only
  once the full sweep completes and passes `assert_fresh`. A half-swept market
  must never be published to the manifest.

**yfinance is split off by execution environment, not by capability.** Yahoo
rate-limits by IP, and Actions runners sit on datacenter ranges that get
flagged quickly. 40 requests from a residential IP is safe; 12k from a cloud
IP is not. So:

- **Tiingo** — scheduled, runs on Actions, ~12k req/night, source of record.
- **yfinance** — hand-run, runs on Easton's machine only, 15–40 req/day,
  fundamentals enrichment over the Tier 2 shortlist. Never reachable from
  `.github/workflows/`.

Its known limitation, recorded so it is not misused later: **yfinance
fundamentals are current values, not point-in-time.** No as-of date, no
restatement history. Good for screening today's names; wrong for the
historical analog engine, which must use XBRL joined to our own price history.

**Stooq is demoted to two supporting roles**, neither on the critical path:

1. **Offline parser test fixture.** A committed handful of Stooq files lets
   the parser be tested forever without burning Tiingo quota or needing
   network. (The archive itself is never committed — see `CLAUDE.md`.)
2. **Independent cross-check.** A second opinion on Tiingo's numbers for
   spot-validation. Two vendors disagreeing is a signal worth having.

Verified Stooq format (confirmed against the actual archive, 2026-09-04):

- Six US asset-class folders under `data/daily/us/`: `nasdaq stocks`,
  `nasdaq etfs`, `nyse stocks`, `nyse etfs`, `nysemkt stocks`,
  `nysemkt etfs`. `nysemkt` is NYSE American (ex-AMEX).
- **No OTC folder.** Stooq's US bulk is listed-only. It cannot validate the
  sub-$1 OTC band, only listed names heading toward delisting.
- Two package formats. **`_txt` is the ASCII one we want**: one `<ticker>.us.txt`
  per security, CSV content, columns
  `ticker,per,date,time,open,high,low,close,volume,openint`, dates as
  `YYYYMMDD` integers, `per`/`time`/`openint` inert for daily data. `_ms` is
  MetaStock binary (`MASTER`/`EMASTER`/`F*.DOP`) and is **not** usable.
- Close is adjusted, and no unadjusted series is published — which is why
  Stooq could never have been the source of record under the raw-storage
  decision in §4.
- Extraction must sanitize Windows reserved filenames. The archive contains
  `prn.us.txt`, and `PRN`/`CON`/`AUX`/`NUL`/`LPT1` cannot be created on
  Windows. Actions is Linux; local dev is not.

### EDGAR form types (Tier 1 triggers)

| Form | Meaning |
|---|---|
| Form 4 | Insider transaction. Cluster = multiple execs buying in 72h. |
| 8-K Item 1.01 | Material definitive agreement signed — often the merger. |
| 8-K Item 2.01 | Acquisition completed. |
| 8-K Item 2.02 | Earnings release. EX-99.1 holds the press release. |
| S-4 | Registration for stock-for-stock merger. |
| DEFM14A | Merger proxy. Contains the banker's valuation work. |
| SC 13D | >5% stake with intent to influence. Often the opening move. |
| SC TO-T | Tender offer. |
| SC 13E-3 | Going private. |

### Private company (all bulk, no scraping)

| Source | Gives | Cadence |
|---|---|---|
| **DOL Form 5500** | Sponsor name, EIN, address, NAICS, participant counts (employee proxy), plan assets. ~800k plans, back to 1999. | Zipped CSVs, ~1st of month |
| **SAM.gov / USAspending** | Federal contract awards — real revenue for contractors | API + bulk |
| **FMCSA** | Fleet size, safety records, operating authority (trucking/logistics) | Bulk |
| **OSHA** | Facility inspections, site-level employee counts | Bulk |
| **EPA (ECHO / FRS)** | Facility registry, permits, physical scale (industrials) | Bulk + API |
| **State contractor licensing** | License issue dates → real entity age | Varies |
| **State SoS bulk** | Entity registry, officers, status, formation date | Varies |
| **UCC filings** | Liens, and especially *terminations* (debt paid off) | Varies |

**Mature-target screen** — replaces the unbuildable "aging owner" filter, since
owner age appears in none of these sources:

> entity age > 15 years
> AND terminated UCC-1 with no replacement financing
> AND flat-to-declining Form 5500 participants over 3 years

Every input is a free bulk file.

---

## 3. Volatility screens

Three price bands, run separately: **sub-$1**, **$1–$10**, **$10+**. Top 20
gainers and top 20 losers by percent move in each. Parallel set gated on >$5M
average dollar volume.

Data hygiene, not risk filtering:

- **Screen on adjusted prices, store raw ones.** Reverse splits are constant
  in the sub-$1 band and read as −95% days if unadjusted. But adjusted history
  is *retroactively rewritten* by every split, which would make year-partitioned
  Parquet quietly diverge from its source — the nightly job never touches
  `prices_2019.parquet`, so a split today would silently corrupt it. Storing
  raw OHLCV plus a `corporate_actions` table and applying the cumulative
  adjustment factor in the query keeps history immutable and append-only,
  makes a bad adjustment a fixable bug rather than a re-download, and keeps
  the year-partitioning scheme honest.
- Prices are `DECIMAL(18,6)` in Parquet and `Decimal` in Python — **not**
  integer cents. Cents cannot represent $0.0002, and the sub-$1 band is a
  headline feature. Six decimal places reach $0.000001.
- Store tick-count move alongside percent. $0.0002 → $0.0003 is +50% and one
  tick.
- Flag scheduled earnings dates so calendar moves separate from surprises.
- Consider a $0.01 sanity floor.

---

## 4. LLM routing

~95% of calls are cheap classification. Route by difficulty in
`llm/router.py`.

| Tier | Provider | Use for |
|---|---|---|
| Cheap | Local Ollama, Groq | Classify filings, extract fields, tag entities |
| Mid | Cerebras, GitHub Models | Summarize filings, draft briefs |
| Good | Google AI Studio (Flash) | 10-page decks, narrative analysis |

Approximate free ceilings — verify, these move:

- Google AI Studio: ~1,500 req/day on Flash, no card. Inputs may be used for
  training outside EU/UK/EEA.
- Groq: 30 req/min, ~1,000/day on 70B, up to 14,400/day on smaller models
- Cerebras: ~1M tokens/day, built for batch
- GitHub Models: frontier models, tied to GitHub account
- Mistral Experiment: ~1B tokens/month, requires opting into training

Jobs go to `job_queue` with a priority. Actions drains N per run inside the
daily budget. Merger filings jump the queue; backfill takes leftover quota
after the day's work.

---

## 5. Build order

### The UI track

Runs *alongside* the weekends, not after them. This was in the original
requirements, got dropped, and every feature since has shipped headless —
which is how you end up with twenty-four screen lists that exist only as
terminal scrollback and a digest nobody scheduled.

**A feature is not done until its panel exists.** That is a third exit
criterion on every weekend from here, alongside "it runs" and "corrupting the
data turns it red". A placeholder panel counts, provided it says what it is
waiting for.

#### Shape

`mr dashboard` writes **one static HTML file** and opens it in the browser.
No server, no build step, no framework, no network at view time. Data is
embedded as JSON in the file rather than fetched, because a `file://` page
cannot fetch its neighbours and adding a server to work around that would
trade the whole point of the constraint for nothing.

**Never GitHub Pages, and never a Release asset.** The screens are computed
from Tiingo prices, so publishing them is redistribution — the same boundary
that put `prices_eod_raw` in R2 and keeps FRED's ICE series local. The output
path is gitignored. A test asserts the dashboard writer has no publish path,
the same way `sources/fred.py` has none.

#### The shell is the map

Panels are declared up front — including the ones that do not exist yet — and
each renders one of three states:

| state | meaning |
|---|---|
| **live** | data is present and current |
| **waiting** | built, but the data it needs has not landed. Says which data. |
| **not built** | on the roadmap. Says which weekend. |

The point is that the dashboard is a picture of the whole system rather than
of the parts that happen to work. An absent panel is indistinguishable from a
broken one; a panel that says "waiting on the 10-year backfill" is not.

#### Panels

| panel | state today | needs |
|---|---|---|
| Health | live | — |
| Macro | live | — |
| Screens (24 lists, ungated collapsed) | live | — |
| Company names on screen rows | live | 54% coverage; the rest need W4 entity work |
| EDGAR filing feed | live | — |
| Form 4 clusters | W3 | cluster detection |
| Ticker detail — price chart | waiting | 10-year backfill |
| Day-over-day / NEW markers | waiting | 2+ sessions of universe history |
| Liquidity gate (12 of the 24 lists) | **waiting, see below** | trailing-window ADV |
| News | not built | W3 |
| 8-K deals | LIVE | W3-T3 |
| Deal multiples | not built | beyond |
| Historical outcomes | LIVE | W3-T3 |
| DCF / 3-statement | not built | beyond |
| Pitch decks | not built | beyond |

#### Blocking issue, ahead of the backfill

`screens/volatility.py` computes average dollar volume with **no time
window** — it averages every row in the partitions it reads. With three
sessions loaded that is accidentally a recent average. With 2025 and 2026
full it becomes a ~1.7-year average, and a name that traded $50M/day in early
2025 and $200k/day now passes the >$5M gate. Twelve of the twenty-four lists
are gated on that number.

Fix the window before the backfill, not after: the gate silently changes
meaning the moment the data arrives, and nothing fails.

### Weekend 1 — Skeleton and prices

Deliberately boring. Every later weekend assumes this layer is trustworthy.

Ordered so the riskiest unknown is tested before anything depends on it.

| # | File | Done when |
|---|---|---|
| T1 | *spike, throwaway* | DuckDB range-reads a Parquet from **R2** and from a Release asset — and fetches only part of the file |
| T2 | `pyproject.toml`, `cli.py` | `uv sync && uv run mr --help` lists every subcommand; unimplemented ones exit non-zero |
| T3 | `manifest.py` | `get("prices_eod_raw","2026")` returns a URL; unknown dataset **raises**, never returns `None` |
| T4 | `storage.py` | `read_dataset()` works against T1's object; no literal URL anywhere in `src/` |
| T5 | `freshness.py` | `assert_fresh` raises on zero rows, stale max-date, missing column; does *not* raise on a Friday run read on Monday |
| T6 | `selftest.py` | `mr selftest` exits 0; `mr selftest --inject-staleness` exits non-zero |
| T7 | `sql/001_init.sql` | Tables exist; inserting the same signal twice is a no-op |
| T8 | `sources/stooq.py` | Parses a real `_txt` zip; runs twice with identical row counts; golden-file test on one ticker |
| T9 | `sources/tiingo.py` | Chunked, checkpointed sweep; killing it mid-run and re-invoking resumes rather than restarts |
| T10 | `.github/workflows/prices.yml` | Manual dispatch green; injected staleness red |
| T11 | `tests/` | Green with no network and no credentials; includes the test that every `sources/` module calls `assert_fresh` |

Notes:

- Parquet has no upsert. "Idempotent" here means read the partition, concat,
  dedupe on `(ticker, date, source)` keeping max `ingested_at`, rewrite the
  file. ~3M rows for a current year — seconds in DuckDB.
- The manifest row is written **only after** a successful upload *and* a
  passing `assert_fresh`. A failed run must never leave the manifest
  advertising data that isn't there.
- Nothing in Weekend 1 populates `companies`. Prices are ticker-keyed until
  Weekend 2's `sec_tickers.py`. That is fine and expected.

**Exit — two criteria, and the second matters more:**

1. `select * from prices where date = yesterday` returns the whole market,
   from a manifest-resolved location.
2. **Corrupting the data turns the pipeline red.** Truncate the Parquet to
   zero rows, or freeze the max date a week back, and the nightly Action
   fails loudly. If it goes green, Weekend 1 is not done regardless of how
   good the prices look.

### Weekend 2 — Screens and identity

- `sources/sec_tickers.py` — `company_tickers.json` → CIK ↔ ticker, seeds
  `companies`
- `entities/resolve.py` — normalization and fuzzy matching
- `screens/volatility.py` — three price bands, gainers and losers, adjusting
  raw prices at query time via `corporate_actions`
- `sources/fred.py` — 10-year Treasury, credit spreads
- `digest.py` — first daily email to yourself

**UI (retrofit — this weekend shipped headless):**

- `U0` `dashboard/shell.py` — `mr dashboard`, the panel registry, the three
  render states, gitignored output
- `U1` health, macro and the 24 screen lists as live panels; ungated lists
  collapsed by default; sort and filter by band and security type
- `U2` `.github/workflows/digest.yml` — the digest is built and reviewed but
  nothing schedules it. Triggered by `prices.yml` completing, not by its own
  clock: a digest rendered mid-sweep is a digest of half the market.

**Exit:** a daily email with six top-20 lists you'd actually read, and the
same lists in a browser without a terminal.

### Weekend 2.5 — Backfill and the liquidity gate

Pulled out of "Beyond" because everything comparative waits on it, and
because two things must land in the right order.

- `T1` trailing-window ADV in `screens/volatility.py` — **before** the
  backfill (see the UI track)
- `T2` 10-year backfill, 2016-01-01 to the last complete session, aligned to
  year boundaries
- `U3` ticker detail panel — price chart per name, activated by T2
- `U4` day-over-day and NEW markers move from *waiting* to *live*

**Exit:** clicking a ticker shows ten years of bars, and the liquid lists mean
what they say.

### Weekend 3 — Sentinels

- `signals/edgar_rss.py` — poller, filtered by form type
- `signals/form4.py` — parser + cluster detection (multi-exec, 72h window)
- `signals/deals.py` — 8-K Items 1.01/2.01 → `deals` table. **Measure the
  base rate before building the extractor**: Item 1.01 is "Entry into a
  Material Definitive Agreement" and only ~16% of it is M&A (949 8-Ks,
  2026-08-31 to 09-04). EX-2.x is the primary classifier — Reg S-K 601(b)(2)
  reserves exhibit 2 for a plan of acquisition, so the filer already did the
  work — with the text heuristic second. Store both and whether they agreed;
  disagreement is a review queue, not an error.
- `screens/outcomes.py` — forward returns at +1/+5/+30 **trading sessions**
  against a benchmark. Pure SQL, no LLM, so it precedes the expensive
  machinery rather than justifying it afterwards. Anchor on the date the
  event became *public* (a Form 4 is filed two business days after the
  trade), and read the survivorship note in CLAUDE.md before believing any
  number it produces.
- `sources/sec_suspensions.py`
- `sources/gdelt.py` and `sources/finnhub_news.py` → `signals`

**UI:**

- `U5` filing feed panel — the seven watched form types, newest first
- `U6` Form 4 cluster panel — **two lists, not one**: officer/director
  clusters and 10%-holder clusters, kept apart for the same reason ETFs are
  kept apart from stocks. Dollar-weighted, plan purchases flagged.
- `U7` news panel — replaces the "not built" placeholder
- `U11` 8-K deals panel — ships with W3-T3, not after it
- `U12` historical outcomes panel — ships with the study

**Exit:** a merger filing lands and appears in your digest *and on the
dashboard* the same day.

### Weekend 4 — Private company data

Most unknowns of the four. Form 5500 sponsor names are messy — DBAs, legal
entity names, and subsidiary rollups all differ from how a company is known.
Fuzzy match into a review queue; never auto-merge above a threshold without a
human-confirmable record. Budget more time than the other three weekends.

- `sources/form5500.py` — bulk loader → Parquet, partitioned by year
- Entity resolution: sponsor names → `companies`
- Participant-count time series and YoY deltas
- `sources/usaspending.py` — contract awards
- `screens/mature_target.py` — v1

**UI:**

- `U8` private-company panel — NAICS, employee count, three-year trend
- `U9` entity review queue — the fuzzy sponsor-name matches, confirmed or
  rejected by hand. This one is a *working* surface rather than a readout,
  and it is the panel most likely to justify the whole track: the alternative
  is resolving 5500 sponsor names in a terminal.

**Exit:** query private companies in your target NAICS by employee count and
three-year trend, and clear a review queue without writing SQL.

### Beyond

- **XBRL normalization** — the real project. Budget a month. `tag_map.py`
  maintained by hand, branch by SIC code.
- **Seed backfill** — 10 years of M&A 8-Ks, ~5,000 docs (not hundreds of
  thousands, because you filter by item code before embedding). Drains on
  leftover quota over a few weeks.
- **Outcome distributions** — forward-return join at +1d/+5d/+30d. Pure SQL
  against local price history, zero LLM calls. Can be built before any
  embeddings exist.
- **Similarity ranking** — deterministic filter first (SIC, size bucket, cash
  vs stock, era), embeddings only to rank within the result.
- DCF / 3-statement engine
- python-pptx deck generation
- FMCSA, OSHA, EPA, state licensing, state SoS/UCC as sectors demand

**UI, paired to each of the above rather than trailing them:**

- `U10` XBRL fundamentals panel — ships with `tag_map.py`, not after it. The
  tag map is maintained by hand and branches by SIC code; a panel showing
  which tags resolved and which fell through is the fastest way to find the
  next branch it needs.
- `U11` 8-K deals panel — shipped with W3-T3, replacing the placeholder
- `U12` deal multiples — the outcome half shipped in W3-T3; multiples wait
  on target financials, which are disclosed in under 10% of deals
- `U13` DCF / 3-statement panel
- `U14` deck preview — the generated pitch deck, before it is a file

The placeholders for all of these exist from `U0`. They say "not built yet"
and which weekend, so the shell is a roadmap you can look at rather than one
you have to remember.

---

## 6. Known gaps

**Unsolved:**

- **Earnings call Q&A transcripts.** No free source. 8-K Item 2.02 EX-99.1
  gives the prepared release but not the analyst Q&A, which is where the tone
  shifts you want actually show up. ~40% of the module achievable at $0.
- **Publicity signals.** Truth Social has no API; X pricing is prohibitive.

- **OTC price coverage.** Stooq's US bulk is listed-only (verified). Whether
  Tiingo covers the OTC names in the sub-$1 band is **still unverified** — see
  §0. Until it is, treat the sub-$1 band as listed-only and do not assume the
  screen sees the whole penny universe.

**Hard but doable:**

- **XBRL normalization.** Tag names vary for the same concept across filers and
  years. History mostly starts ~2009. Banks, insurers, and REITs need separate
  handling or they silently produce garbage.
- **Form 5500 entity resolution.** Expect a manual review queue permanently.
  Note this implies a human-confirmation interface that is not yet specced or
  designed anywhere in this document.
- **Weekend 4 is not a weekend.** Form 5500 sponsor resolution across ~800k
  plans is a multi-week project. Weekends 1–3 build the equities product;
  Weekend 4 starts a second product from zero.

**Operational:**

Six free tiers means six ways to silently stop returning data while the job
still exits green. Every stage asserts row count and max timestamp. The LLM
router degrades to the next provider rather than killing the run.
