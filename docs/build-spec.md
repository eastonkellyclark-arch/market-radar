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
| **DOL Form 5500** | Sponsor name, **EIN (on 100% of filings)**, address, NAICS, participant counts (employee proxy), plan assets. **Two datasets: `F_5500` main form and `F_5500_SF` short form. 1,023,597 plans for plan year 2024 alone** (225,591 + 798,006), back to 1999. The short form is where small private employers are. | Zipped CSVs, refreshed monthly; plan years lag, so the newest complete year trails by ~1.5 years |
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
| **declined** | measured and decided against. Says what the measurement was. |

`declined` is a separate state from `not built` because only one of them is a
promise. News read "planned for Weekend 3" for a day after news had been
measured and declined — the same distinction as `absent` against `unmapped` in
the XBRL coverage and `lapsed` against `declining` in the Form 5500 series,
and it goes wrong the same way: collapsing them keeps the count and loses the
reason.

The point is that the dashboard is a picture of the whole system rather than
of the parts that happen to work. An absent panel is indistinguishable from a
broken one; a panel that says "waiting on the 10-year backfill" is not.

#### Navigation

One panel is in the DOM at a time, behind a sidebar that lists all twenty
grouped by section, each with its state glyph. The open panel is remembered
in `localStorage` and is linkable as `#panel-<id>`; a hash beats the stored
panel, because a hash was asked for and the stored one is only where you
happened to be.

Two properties hold it together, and both were learned by breaking them:

- **The page renders every panel, then the runtime takes them out.** So with
  the script off it degrades to one long readable document rather than an
  empty frame — the same property the screens panel keeps by never hiding a
  list in the markup. The sidebar is plain in-page anchors for the same
  reason.
- **A panel's script is a binder, not an IIFE.** Each is registered on
  `window.__MR_BINDERS__` and re-run against the freshly injected root after
  every switch. The old elements leave with the old `innerHTML`, so re-binding
  cannot double up, and a binder that throws is caught and logged rather than
  taking the other thirteen with it. What does *not* survive a switch is a
  panel's in-page state — a checked gate, a sort order — which is the price of
  re-injection and is worth it at this size.

#### The page is tested by running it

659 tests were green on a page where no tab worked and the ticker script died
on its first statement with a `ReferenceError`. Every one asserted on the
*text* of the generated HTML — the markup said `data-axis="band"`, the script
said `addEventListener`, and nothing anywhere ran the two together. Three
breakages hid in that gap at once.

So `tests/test_dashboard_js.py` loads the rendered page in jsdom, with the
scripts running, and drives it with real click events; the assertions are about
what *changed*. Two origins, because a `file://` page is an opaque origin where
`localStorage` throws outright and a guard nothing exercises is a guess.

Two rules keep it honest, and both are the same rule:

- **Missing tooling fails, never skips.** A skipped test reports the same green
  as a passing one to anyone reading a summary line.
- **So CI has to supply the tooling.** `.github/workflows/tests.yml` exists for
  that — it is also the first workflow to run the suite at all, since `prices`
  and `digest` are data jobs. Red on a runner that was never given `npm ci` is
  a broken build, not a caught bug. A step there also asserts the DOM tests
  were *collected*, because a renamed file would otherwise leave the workflow
  green with the browser-side checks silently gone.

Node is test-only. Nothing in `src/` touches it and neither data job does: the
dashboard has no build step and still ships as one static file.

The screens panel is **two tab axes** rather than twenty-four stacked
collapsibles: security type and price band, which are the two axes a list is
in exactly one of. Direction is deliberately not an axis — gainers and losers
are read together, because a name near the top of one and the bottom of the
other is the case worth seeing. The $5M ADV gate is an in-place toggle for a
related reason: it changes which names qualify, not which question is asked.

#### Panels

Grouped the way the sidebar groups them. "State" is what the panel resolves
to on a fully loaded system — the probe decides at render time, and a panel
that cannot be live says which data it is waiting on.

**This table is parsed by `tests/test_dashboard_spec.py`**, which resolves
every panel against a fully-loaded context and fails the build where the two
disagree on a panel's section or state. It is a doc, so it is not the source
of truth — `shell.PANELS` and the probes are, and the shell must not read
markdown to render itself. But both of the stale strings that prompted this
drifted in the same direction: the table was updated and the code was not, and
nothing compared them. Editing a state here now means editing the probe, or
the build goes red naming both.

| section | panel | id | state | notes |
|---|---|---|---|---|
| Markets | Health | `health` | live | sweep coverage, staleness per source, entity counts |
| Markets | Macro | `macro` | live | DGS10 and the two ICE BofA spreads. Local-only, per the FRED rule |
| Markets | Volatility screens | `screens` | live | 24 lists, two tab axes — see *Navigation* |
| Markets | Company names | `names` | live | 54% coverage; the rest need W4 entity work |
| Markets | Liquidity gate | `liquidity` | live | 30-session trailing ADV. Was the blocking issue below; it is closed |
| Markets | Ticker detail | `ticker` | live | chart, bars, actions and gap markers, split-adjusted at read time. Waited on the backfill, then on U3, and on neither since |
| Markets | Day-over-day | `dod` | live | NEW marks a name absent from the same list last session |
| Filings | EDGAR filing feed | `filings` | live | the seven watched form types |
| Filings | Form 4 clusters — officers & directors | `clusters_insider` | live | W3 |
| Filings | Form 4 clusters — 10% holders | `clusters_tenpct` | live | a separate panel, not a filter: the medians are 78x apart, so one floor cannot serve both |
| Filings | 8-K deals | `deals` | live | W3-T3. Both classifiers shown; disagreements are a review queue |
| Filings | News | `news` | declined | measured 2026-09-10 and declined — see *News: measured, declined*. The panel carries the measurement, not a weekend |
| Private | Private companies | `private` | live | W4 |
| Private | Mature targets | `mature` | live | W4 |
| Private | Entity review queue | `review` | live | W4 |
| Analysis | XBRL fundamentals | `xbrl` | live | six concepts with their own coverage figures, the five statuses behind each miss, and the tags to add. U10, shipped with `tag_map.py` rather than after it |
| Analysis | Deal multiples | `multiples` | not built | beyond |
| Analysis | Historical outcomes | `outcomes` | live | W3-T3. The survivorship caveat renders beside the number, not in a docstring |
| Analysis | DCF / 3-statement | `dcf` | not built | beyond |
| Analysis | Pitch decks | `decks` | not built | beyond |

#### Blocking issue, ahead of the backfill

`screens/volatility.py` computes average dollar volume with **no time
window** — it averages every row in the partitions it reads. With three
sessions loaded that is accidentally a recent average. With 2025 and 2026
full it becomes a ~1.7-year average, and a name that traded $50M/day in early
2025 and $200k/day now passes the >$5M gate. Twelve of the twenty-four lists
are gated on that number.

Fix the window before the backfill, not after: the gate silently changes
meaning the moment the data arrives, and nothing fails.

**Closed.** The gate is a 30-session trailing average, and a name with fewer
sessions than that stays in the ungated lists and is counted there rather
than being silently dropped or silently passed. Kept above rather than
deleted because the shape recurs: a number that is accidentally right on
small data and wrong on full data fails no test on the way between.

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
  trade).

  **Every excess return this produces is biased downward, and the bias runs
  in the same direction as the number.** The price universe is current
  listings only, so a target that was acquired has no +30-session close and
  drops out of the study — but that drop is *not random with respect to the
  outcome*. Completing the deal is precisely what delists the company, while
  a deal that collapses leaves the target trading and in the sample. So the
  events that survive to be measured are weighted toward the ones that
  failed, and failed deals are exactly the population that gives back the
  announcement pop.

  A negative excess return here is therefore partly a measurement of who is
  left, not of what deals do. The correction goes **up**, by an unknown
  amount — unknown because closing it needs a point-in-time universe, which
  is a data purchase rather than a query. The funnel's `priced` count shows
  how much of the population was lost; it does not show which way. This
  paragraph is why the caveat renders next to the figure in `U12` and in the
  CLI rather than living in a docstring.
- `sources/sec_suspensions.py`
- ~~`sources/gdelt.py` and `sources/finnhub_news.py`~~ — **declined 2026-09-10 after measurement.** See "News: measured, declined" below.

**UI:**

- `U5` filing feed panel — the seven watched form types, newest first
- `U6` Form 4 cluster panel — **two lists, not one**: officer/director
  clusters and 10%-holder clusters, kept apart for the same reason ETFs are
  kept apart from stocks. Dollar-weighted, plan purchases flagged.
- ~~`U7` news panel~~ — **not built. News was measured and declined; see
  below.**

**Exit:** a merger filing lands and appears in your digest *and on the
dashboard* the same day.

#### News: measured 2026-09-10, declined

Recorded here so it is not revisited from scratch. `sources/gdelt.py` and
`sources/finnhub_news.py` are **not built on purpose**, and the reason is a
measurement rather than a preference.

GDELT's DOC 2.0 API is free and needs no key. Over 7 days, 18 companies
sampled from our own universe, 1,800 articles:

| measure | result |
|---|---|
| volume | 14.3 articles per company per day |
| company named in the headline | **13.4%** |
| stock-commentary domains | 4.5% |
| listicle-shaped titles | 3.1% |

86% of what a company-name query returns does not name that company in the
headline — they are passing mentions in article bodies. The domain mix tells
the rest: `163.com` supplied 218 of NetEase's 250 hits (NetEase's *own*
portal), then `themarketsdaily.com` 130, `dailypolitical.com` 97,
`tickerreport.com` 88. Home Depot's top result was "Tips on Selling Stuff
from a Guy Who Sold a Lot of Stuff". Results are multi-language and
unfiltered.

The deciding test was lead time, because M&A detection here is already by
form type and news only earns a place if it *precedes* the filing. For deals
drawn from the `deals` table, GDELT was asked what it carried in the ten days
before the 8-K:

- 4 of 7 resolved deals had prior coverage, 1 same-day, 2 none at all
- **but not one of the four "leading" articles was about the deal.** They
  were: two insider-transaction reports (which Form 4 gives us directly,
  parsed and dollar-weighted), one unrelated corporate PR, and one
  stock-performance filler piece.

So the lead is ambient coverage, and where it names a real event, that event
is one we already detect from the filing itself, more reliably.

Two further constraints found in passing:

- **The API cannot be swept.** GDELT rate-limits at one request per five
  seconds and tightens under sustained use: 12 of 30 companies failed at
  6-second pacing, 8 of 15 at 10-second. 14,000 tickers is 19 hours at the
  stated limit and not achievable in practice.
- **The bulk path does not rescue it.** `data.gdeltproject.org/gdeltv2/`
  publishes the whole GKG every 15 minutes as a ~4 MB zip, free and
  unmetered, which *would* solve the rate limit. It does not solve the base
  rate. 86% noise does not improve at volume, it only gets bigger.

If this is reopened, the thing to re-measure is the lead time, not the
volume. News becomes worth building the day it names an event before the
filing does.

### Weekend 4 — Private company data

**Measured 2026-09-10 before building, and the measurement changed the
design.** The old plan here was "fuzzy match ~800k sponsor names into a
permanent review queue". That is now known to be the wrong shape, and the
numbers are recorded below so it is not proposed again.

Plan year 2024, both forms, 1,023,597 plans and 858,480 distinct sponsors:

| tier | sponsors | share |
|---|---|---|
| EIN match to an SEC filer (authoritative) | 22,141 | 2.58% |
| exact name match | 13,008 | 1.52% |
| normalized name match | 41,913 | 4.88% |
| any of the three | 45,547 | 5.31% |
| no match — the private population | 812,933 | 94.69% |
| EIN match to a *listed* filer | 2,159 | 0.25% |

Two findings decide the build:

**EIN is on every record.** Zero of 1,023,597 filings lack one. NAICS is on
96–99.9%, state on 100%, and a DBA name on only 1.8% — so the DBA problem the
old note warned about is real but rare, and it is not on the join path at
all. Resolution keys on EIN.

**Name matching is wrong more often than right.** Because EIN gives ground
truth, name matching can be *scored* rather than assumed. Of the 22,141
sponsors with an authoritative EIN match, exact name recall is 39.3% and
normalized name recall 83.6% — but normalized name also produced 23,406
matches with no EIN match, so its **best-case precision is 44.2%**. Fuzzy
matching sits strictly below that. Collisions show why: 11 SEC filers share
the normalized key `'energy'`, 9 share `'capital'`.

A matcher that is wrong more than half the time is worse than no matcher,
because the errors are invisible. So there is **no fuzzy matching on the
public-match path**.

- `sources/form5500.py` — bulk loader → Parquet, partitioned by plan year.
  **Both forms.** `F_5500` is the main form (100+ participants, 225,591
  filings for 2024) and `F_5500_SF` is the short form (under 100 participants,
  798,006 filings). The short form is the private population; loading only
  the main form gets a quarter of the data and the wrong quarter.
- Entity resolution: **EIN → `companies`, never sponsor name.** Name matches
  that disagree with EIN go to the review queue.
- **DFE filings are flagged, not dropped.** 9,805 of the main form's 225,591
  filings are Direct Filing Entities — master trusts, collective investment
  funds, pooled separate accounts (`TYPE_DFE_PLAN_ENTITY_CD` in C/P/M/E/G/D).
  They are trustees, not employers, and they dominate any plan-count-weighted
  view: sorted by plans, the top of the "private" population is Transamerica
  Life, State Street Global Advisors Trust and BNY Mellon. Excluded from
  employer lists, marked as a category rather than deleted — same treatment
  as the FUND flag on Form 4 clusters and the SPAC deal type.
- **Partial plan years are visible, never silently thin.** Filings lag the
  plan year: 2026 does not exist yet and 2025 is a third the size of 2024
  (10 MB against 28 MB) because it is still being filed. 2024 is the newest
  complete year. A count that is small because the year is young must say so.
- **Participant-count time series and YoY deltas.** Built across plan years
  from the published sponsor parquets, not by re-parsing the archives: the
  EIN resolution and the DFE flag already live in those files, and a series
  that disagreed with the private-company panel about who is private would
  be worse than no series.

  **"Participants fell" and "stopped filing" are different columns, and that
  is the whole design.** A sponsor missing from a later plan year may have
  terminated the plan, been acquired, changed EIN, dropped below the filing
  threshold, or -- overwhelmingly, in the newest year -- simply not filed
  yet. Filings lag the plan year by about eighteen months, so 2025 held a
  third of 2024's filings while it was still being filed; treating an
  absence as a zero would manufacture a cliff for most of the file, and a
  screen looking for shrinking headcount would sort exactly those to the
  top. So `f5500_trend` carries:

  | column | says |
  |---|---|
  | `status` | `filing` or `lapsed` — presence, never a direction |
  | `trend` | `growing`/`flat`/`declining`/`unknown`, measured **only between plan years the sponsor filed, and only complete ones** |
  | `pending_years` | absences in a year still being filed. No information |
  | `gap_years` | complete years skipped between two that were filed. A real oddity |

  `unknown` is a real answer and the most common one: a sponsor with a single
  filed year has no trend, and inventing one out of an absence is the bug.

  **The same problem exists one level down, at the plan, and the first build
  shipped with it.** A sponsor's *set* of filed plans is not stable between
  years — plans open, merge, terminate, or get filed late — so comparing
  everything it filed in 2022 against everything it filed in 2024 compares
  two different things and calls the difference headcount. Edward Don &
  Company filed two plans for 2022 and one for 2024 and read as −46%; The
  Juilliard School's largest single plan went 1,473 → 500 → 981 while its
  total barely moved, because with six plans *which* one is largest keeps
  changing. So the trend compares only the plans present at **both** ends,
  keyed on `(ein, plan_num)` — DOL's own identifier for a plan — and
  `plans_added`/`plans_dropped` report the rest as counts.

  **And the obvious participant column is not headcount.** Measured
  2026-09-10: `TOT_PARTCP_BOY_CNT` counts retirees and separated
  ex-employees who still hold a balance. Active is 78% of total on the main
  form and 82% on the short form, and for an old institution far less —
  Boca Raton Regional Hospital reports 934 participants and 388 active, J M
  Smith 890 and 362. A trend on the total measures a pension plan paying
  people out, which is what an old employer does whether or not it is
  shrinking; the first version of the mature-target screen ranked on it and
  returned a list of hospitals, universities and charities. The series runs
  on `TOT_ACT_PARTCP_BOY_CNT` (95.2% of main filings, 99.9% of short), and a
  plan that does not report it is left out of the comparison rather than
  counted as zero.

- **Entity age, as a floor.** `PLAN_EFF_DATE` gives the effective date of the
  oldest plan a sponsor still files. That bounds how long the company has
  existed and is never its age: a firm founded in 1971 whose 401(k) started
  in 1985 reads as 1985, and one that terminated its original plan and
  opened a new one in 2019 reads as 2019. The error only ever runs one way —
  it *understates* age, hiding targets rather than inventing them — which is
  what makes it usable as a screen input and unusable as a fact. Both ends of
  the column are bounded because both hold typos: it runs 1876-11-11 to
  2027-08-01 in the 2024 file, a century before ERISA at one end and in the
  future at the other.

  The real number needs state SoS or UCC filings. That is bulk state data
  this project has not touched, and it is deliberately not a blocker: the
  floor is the proxy until then.

- `screens/mature_target.py` — v1. Old, sized, still filing, not growing, and
  matching no SEC filer. Four filters, each dropping a population for a
  stated reason, with `population()` reporting what each one removed — a
  short list is either selective or broken, and the funnel is the difference.
  `score` is a documented sort key over three visible components, not a
  probability.
- `sources/usaspending.py` — contract awards. **Deferred**, and additive
  rather than blocking: nothing above depends on it.

**UI:**

- `U8` private-company panel — NAICS, employee count, three-year trend, with
  DFEs filterable as their own category. The trend cell renders `lapsed` and
  `pending` as themselves rather than as a direction, and the sparkline draws
  a year with no filing as a *gap* rather than a zero-height bar — the two
  look identical and mean opposite things.
- `U9` entity review queue — **the ~23,000 sponsors whose name matched an SEC
  filer while their EIN did not.** Not 800k names: the private population has
  nothing to resolve against and needs no review. A *working* surface rather
  than a readout.
- `U15` mature targets — the screen above, with age shown as `≥N years` and
  its direction of error stated on the panel.

**Exit:** query private companies in your target NAICS by employee count and
three-year trend, and clear a review queue without writing SQL.

### The screen failure mode

Five defects were found in the Form 5500 screen on 2026-09-10, **after** the
pipeline was green: tests passing, loads idempotent, output deterministic,
dashboard rendering. Four of the five were the same mistake, and it is worth
naming because it is not specific to Form 5500.

**The list contained something other than what the screen claimed to
measure.**

| what was in the list | what the screen claimed | why it looked fine |
|---|---|---|
| sponsor totals over a plan set whose membership changed year to year | headcount change | Edward Don filed 2 plans for 2022 and 1 for 2024: −46% |
| total participants, including retirees and separated ex-employees | employees | active is 78% of total, and far less at an old employer |
| boards of trustees for multiemployer union plans | employers | a trade in decline looks exactly like a firm in decline |
| `1900-01-01`, a placeholder | an effective date | an *LLC* reading as 127 years old |

The fifth was different in kind and worse in character: the filter fixing the
third defect used the main form's code vocabulary against the short form,
where the same-named column means something else, and removed 800,287
sponsors instead of 4,502.

Three properties they share, and each one is a lesson that generalises:

**A green suite cannot catch this.** A test encodes what its author believed
about the data. Every one of these defects *was* a gap in that belief, so the
tests were written to assert the wrong thing and passed doing it. Determinism
checks, idempotency checks and freshness assertions all held throughout —
they verify that the pipeline does the same thing every time, not that the
thing is the right one.

**The broken version looks better than the correct one.** This is the part
that makes the failure mode dangerous rather than merely annoying. The
800,287-sponsor filter produced a list of old colleges and firemen's relief
funds: plausible, coherent, nothing visibly wrong. The correct filter
produced a messier list. A reviewer eyeballing the output would have
preferred the broken one.

**Only the population counts said otherwise.** Four of the five were caught
by reading the output and disbelieving it, which does not scale and depends
on knowing the domain. The fifth — the only one caught cheaply and
immediately — was caught by a funnel line showing 922,340 → 122,053 where a
few thousand were expected.

#### So: every screen ends with a funnel

The rule, and it is the exact parallel of the freshness assertion on every
loader:

> **A short list is either selective or broken, and printing the surviving
> count at each stage is what tells you which.**

`assert_fresh` refuses to let a job exit green on empty data. The funnel
refuses to let a screen report a plausible list without saying what it
discarded to get there. Same failure, one step later in the pipeline.

`screens/funnel.py` holds the shared type. Every screen builds one —
volatility, Form 4 clusters, action audit, outcomes, mature targets — and a
test in `tests/` parses each module and fails the build if one does not,
the same enforcement `assert_fresh` gets. Two things are checked rather than
merely printed:

- **`emptied`** — a stage that took a non-zero population to zero. The list
  is not short, it is gone, and that should be said rather than inferred
  from a blank screen.
- **`collapsed`** — a stage that removed more than 90% of what reached it.
  Not an error; several honest stages do this. It is where a broken filter
  hides, so it is marked and the reader decides.

Each stage carries *why* it exists alongside its count, because a number with
no reason attached is the number nobody checks.

#### And the smaller rules that fell out

- **Before comparing two aggregates across time, ask what the set is made of
  and whether the membership is the same at both ends.** Keying correctly on
  the entity is not enough if the thing being summed underneath it drifts.
- **Read the field definition, not the field name.** When a source offers a
  total and a component, the total is usually a superset of what you want and
  substituting it never errors.
- **Two files from one publisher can use one column name for two
  vocabularies.** Check the code distribution per file before mapping it, and
  treat a filter that removes far more or far less than expected as a bug
  until proven otherwise.
- **A flag whose evidence is invisible is a silent decision.** Where a
  category has to be set aside — DFEs, multiemployer plans, nonprofits — the
  row carries *why*, the count is reported, and a filter can bring them back.

### Beyond

- **XBRL normalization** — the real project, scoped below.
- **Seed backfill** — 10 years of M&A 8-Ks, ~5,000 docs (not hundreds of
  thousands, because you filter by item code before embedding). Drains on
  leftover quota over a few weeks.
- **Outcome distributions** — forward-return join at +1d/+5d/+30d. Pure SQL
  against local price history, zero LLM calls. Can be built before any
  embeddings exist.
- **Similarity ranking** — deterministic filter first (SIC, size bucket, cash
  vs stock, era), embeddings only to rank within the result.
- **EX-21 subsidiary extraction** — the named fix for the private list's
  known blind spot. `ARCELORMITTAL TUBULAR PRODUCTS USA LLC` ranks as a
  century-old private employer because the *parent* files with the SEC while
  the subsidiary sponsors the plan, so its EIN matches nothing. Every 10-K
  carries an EX-21 "Subsidiaries of the Registrant" exhibit naming them, so
  the link exists in the filings we already fetch.

  Worth noting what kind of solution this is: EX-21 is **prose, not a
  table** — a list of names and jurisdictions with no EIN and no CIK. So it
  cannot be the join key; it can only produce *candidates* for one. That
  makes it the review-queue shape rather than the EIN shape, and it must not
  become fuzzy name matching on the public-match path by the back door:
  a parent-subsidiary claim goes into `entity_review` with the exhibit text
  as its evidence, and a human confirms it. Scoped that way it is a few
  thousand high-value links rather than a matcher.

  Until it exists, a recognisable corporate name in the private list is a
  prompt to check rather than a finding, and the caveat stays in `U8`.
- **Deal multiples — built, and blocked on the deal population rather than on
  target financials.** `screens/deal_multiples.py`, 2026-09-10. The build order
  assumed this waited on target financials, which are disclosed in under 10% of
  deals. That assumption is wrong in both directions and the correction matters.

  For a *public* target the financials are fully available and **unbiased**:
  every one of Activision, VMware, Twitter, Seagen, Slack, Xilinx, Arena and
  Horizon sits in the XBRL partitions with its filing history ending cleanly
  before its acquisition. XBRL is as-filed and keeps every filer that ever
  filed, so unlike the price history it has no survivorship hole.

  What is missing is the *deal*. Measured: of 2,160 filers whose 10-K history
  ends before 2024, **1.7% appear in the `deals` table at all**, against 50.7%
  of the 4,271 still filing — a thirty-fold gap. An acquisition target is by
  definition a company that stopped filing, so the deal population is missing
  almost exactly the rows a multiples screen needs. None of those eight
  acquisitions is in it. The cause is upstream: `companies` is built from SEC's
  current-only `company_tickers.json` and holds 107 of those 2,160, so any
  population selected through it inherits the hole.

  The screen is correct and produces **ten rows** out of 10,762 deal candidates,
  and its funnel says where they went. Two things it establishes on the way:

  - 84% of filings with a stated value and a pre-deal annual report **filed
    another 10-K afterwards** — they sold a division, not themselves. Dividing a
    division's price by the parent's whole revenue gives a small multiple, and a
    screen ranking cheap deals first would have ranked its own errors first. So
    the target's identity is confirmed from the filing record, never from prose.
  - "Has not filed yet" is not "stopped filing", bounded by the edge of loaded
    history as well as by the calendar. Third outing for that distinction after
    `pending_years` and the XBRL nil tag.

  **The fix is now a query rather than a purchase, for this half of the
  problem.** The XBRL `sub` tables are a point-in-time *filer* universe: 6,431
  CIKs over 2019–2026 including the 2,160 that stopped filing, with name, SIC
  and period. CLAUDE.md says a point-in-time universe is a data purchase — true
  of a point-in-time *price* universe, and no longer true of a filer one, which
  arrived free with the XBRL load. Rebuilding the 8-K deal population against it
  is the next task and is a real sweep, not a query rewrite.

  **Re-run against the rebuilt population, 2026-09-10.** The targeted sweep
  (`mr deals --targets stopped`) took the deals table from 10,764 rows toward
  ~13,500, and deal multiples from **10 rows to 21** at a quarter of the way
  through — with a list that is now recognisably real takeouts rather than noise:
  KEMET at 1.27x (Yageo, 2020), Tallgrass at 4.03x (Blackstone), Instructure at
  7.74x (Thoma Bravo), AquaVenture at 5.41x (Culligan), Habit Restaurants at
  0.80x (Yum!), Ra Pharmaceuticals at 700x (UCB — a correct multiple of a
  meaningless denominator, being clinical-stage).

  Getting there cost two corrections, both of which had produced plausible
  output:

  - **Identity must come from the wide table, not the narrow one.** CleanSpark
    appeared as a 1.18x takeout. It is alive; it had left the *fundamentals*
    table because its SIC moved into a financial class the operating-company
    filter excludes. "Disappeared from the narrow table" is not "stopped
    filing", and only the filer universe — every form, every SIC — tells them
    apart. This is what that universe is *for*.
  - **A company stopping is not the same as this filing being what stopped it.**
    One CIK had twenty deal filings 2019–2025, a stream of $1–31M transactions,
    and every priced one read as a takeout because its 10-K history had ended —
    it filed an 8-K in September 2025. A later filing by the same CIK is proof it
    outlived the earlier transaction, and is now a second guard.

  What survives is **"the last stated deal value a company filed before it
  stopped reporting, over its last reported revenue"** — weaker than "the price
  it was acquired for", and named that way. Two residual error classes, both
  visible in the output rather than silent:

  - *Which filing was the takeout.* Dean Foods shows $48M over $7,329M of
    revenue: it did cease, but that filing is a liquidation asset sale and not
    the DFA purchase. Pier 1 is the same shape. Closing this needs the
    target-side forms — DEFM14A, SC 13E-3, SC TO-T — where the company being
    bought states the price unambiguously. They are in CLAUDE.md's watched set
    and **nothing sweeps them**: `deals.FORM_TYPES` is `("8-K", "8-K/A")` alone.
  - *Partial value extraction.* Anixter reported $400M against a $4.5B WESCO
    deal before the identity guards removed it — the regex found a figure, just
    not the one that mattered.

  Operating income is the other gap: EBITDA is the standard M&A denominator and
  operating income is the available proxy, measured at 91.3% and deferred out of
  the tag map's v1. Adding it is a map edit plus a re-resolve of the 30 quarters,
  which the cached zips make cheap. v1 divides by revenue and net income, names
  them `value_to_*` rather than `EV/*` because the 8-K states a transaction value
  with no net-debt adjustment, and says so beside every number.

- DCF / 3-statement engine
- python-pptx deck generation
- FMCSA, OSHA, EPA, state licensing, state SoS/UCC as sectors demand

### XBRL normalization — scope

**The next thing to build, and the one that gates the rest.** Deal multiples
need target financials, the DCF engine needs statements, and comparable-deal
ranking needs a size bucket. All three read normalized fundamentals, so this
comes before any of them. The seed backfill and similarity ranking do not
depend on it and can drain leftover quota in parallel.

Budget a month of weekends. The month is not spent writing the mapping — it
is spent discovering which mappings are needed and proving the coverage.

#### Measure before designing

Weekend 4's plan was wrong until it was measured, and this one is a bigger
version of the same risk, so **task one is a measurement, not a loader**.
Download two quarters, and answer:

1. How many distinct tags carry each core concept, and what is the coverage
   curve — do the top 5 tags cover 80% of filers, or is it a long tail?
2. What share of filers resolve *every* core concept cleanly? That number is
   the honest ceiling on any screen built from this.
3. How many filers fall into the SIC classes that need separate handling,
   and what do they report instead?
4. How often does a filer change tags between years for the same concept?
   That decides whether the map is keyed on tag alone or on (tag, era).

The measurement decides the design and is recorded beside it, as with Form
5500's 44.2%.

#### Measured 2026-09-10 — seven quarters, 2013q1 to 2024q1

Task one is done and **the measurement changes the scope**, as it did for
Form 5500. Seven `q1` datasets a few years apart, operating companies only
(the exclusion is question 1 below), income-statement top line discovered
from `pre.txt` rather than assumed from a tag list.

**1. A quarter of filers are not comparable at all, and it is stable.**
Not "needs a SIC branch" — genuinely a different statement:

| | 2013q1 | 2024q1 |
|---|---|---|
| operating company | 73.7% | 73.4% |
| bank / credit (SIC 6000–6199) | 10.5% | 9.5% |
| REIT (6798) | 4.3% | 4.8% |
| real estate / holding | 5.6% | 3.8% |
| broker / exchange | 3.2% | 3.0% |
| insurance | 2.6% | 2.6% |
| **not comparable** | **26.2%** | **23.7%** |

These belong in **their own table**, not in the same one behind a branch. A
bank's top line is interest income and its balance sheet does not decompose
into the same parts; a REIT's earnings measure is FFO. Forcing them into an
operating-company shape produces numbers that are present, plausible and
wrong — the exact failure the Form 5500 work spent a day on. v1 covers
operating companies and *says* it covers operating companies.

**2. The revenue tag is not one tag, and how bad that is depends entirely
on the era.** Distinct tags appearing as the income-statement top line:

| | 2013 | 2016 | 2018 | 2019 | 2020 | 2022 | 2024 |
|---|---|---|---|---|---|---|---|
| distinct top-line tags | 419 | 389 | 341 | 251 | 210 | 195 | **133** |
| covered by the top 5 | 71.9% | 69.6% | 70.8% | 87.0% | 91.0% | 91.2% | **93.0%** |

**3. It is one cliff, not rolling churn — and this is the answer to whether
eleven years is worth loading.** Share of filers changing their top-line tag
between consecutive sampled years:

```
2013 -> 2016   20.0%      (three years)
2016 -> 2018   13.8%      (two years)
2018 -> 2019   78.2%      <-- ASC 606
2019 -> 2020   18.2%
2020 -> 2022   15.5%
2022 -> 2024   11.1%      (two years)
```

The tag families make the mechanism explicit:

```
             2013    2016    2018    2019    2020    2022    2024
SalesRevenue* 48.1%  48.0%  48.4%   2.8%   0.1%   0.1%   0.0%
Revenues      23.1%  21.3%  21.9%  33.2%  27.9%  23.7%  24.5%
RevenueFrom-
Contract*      0.0%   0.0%   0.0%  49.7%  58.0%  60.7%  62.2%
```

So the map is **era-keyed with exactly one boundary**, at fiscal years
beginning on or after 15 December 2017. Two mappings, not a rolling mess —
which is the good version of this answer. But the two are not equal work:
the pre-606 era has three times the distinct tags and twenty points worse
top-5 concentration, so **the older half is most of the effort and buys the
worse coverage.** Build post-606 first, and treat pre-2019 as a separate
decision made on evidence rather than a given.

Baseline churn of 11–20% per sampled interval is not nothing either. A map
keyed on tag alone will rot; it needs a coverage report that runs every load,
which is what makes `U10` a requirement rather than a nicety.

**4. The ceiling on all ten concepts together is far below any one of
them.** Operating companies, per concept:

| concept | 2013q1 | 2024q1 |
|---|---|---|
| liabilities | 99.0% | 99.9% — **wrong, see below** |
| operating cash flow | 98.1% | 99.7% |
| assets | 98.0% | 99.7% |
| equity | 96.4% | 98.2% |
| net income | 94.1% | 99.6% |
| shares | 91.9% | 95.1% |
| cash | 91.4% | 96.6% |
| operating income | 82.5% | 91.3% |
| revenue | 82.4% | 86.8% |
| capex | 74.4% | 79.9% |
| **all ten on one filer** | **51.6%** | **64.2%** |

**The liabilities row is wrong and the way it is wrong is the point.** Building
the loader re-measured it at **83.2%**, not 99.9%. The original number was
counting `LiabilitiesAndStockholdersEquity`, which is the balance-sheet total
and equals *assets* — not liabilities at all. That is this document's own
"check what the column counts before naming it" rule, applied to this
document's own measurement, and it went unnoticed because 99.9% is exactly what
a reader hopes for from a balance-sheet total.

The obvious repair is `LiabilitiesAndStockholdersEquity - equity`, which lifts
coverage to 99.0% and was **measured and rejected**: it disagrees with the
stated figure by more than 5% for 5.5% of the filers that report both, and 84%
of those carry temporary or redeemable equity, where the gap equals the
mezzanine amount exactly. Redeemable NCI sits between liabilities and equity,
in neither tag, so the subtraction files it under debt. ProKidney Corp states
$29.2M and derives $1.52B — a 52x overstatement that would top any leverage
screen. Subtracting mezzanine as well would fix 84% and leave the rest wrong
invisibly, which is worse: a derivation that is usually right is harder to
distrust than one that is absent. So liabilities is stated-only, and 17% is a
stated gap rather than a silent error.

Individually most concepts look solved. Together they are not: requiring all
ten halves the population in 2013 and takes a third of it in 2024. **Any
screen demanding a complete row silently discards a third of the market** —
and it will not look like it is discarding anything, which is the whole
lesson from the Form 5500 work. Pick concepts per question rather than
building one wide table and joining against it.

**5. Some filers have no revenue, and that is not a mapping failure.**
Between 4.2% (2013) and 9.6% (2022) of operating companies open their income
statement with an *expense* line — pre-revenue biotech and mining, mostly.
Resolving revenue for them is impossible because there is none, and a
resolver that treats it as a missing tag will chase it forever. Absent and
unmapped are different, exactly as lapsed and declining are.

#### v1 scope — decided 2026-09-10 on the numbers above

**Post-606 only. 2019 forward.** One map, 133 top-line tags, 93% top-5
coverage. Pre-2019 is a separate decision taken once the post-606 half is
working and its real cost is on the table, not a phase two that is assumed
into the plan now. The measurement says the older half is three times the
tags for twenty points less concentration; that is a case to be made later
with the machinery built, not a commitment to make today.

**Operating companies only.** Banks, insurers, brokers and REITs — 23.7% of
2024 filers — are out. Their own table later, or never: they are not a
valuation target here, and building a shape nobody reads is worse than not
building it. What matters is that they are *excluded by name and counted*,
not silently absent, so a later reader knows the table is 76% of the market
by construction rather than by accident.

**Six concepts, not ten:**

| concept | 2024q1 coverage |
|---|---|
| liabilities | 99.9% |
| operating cash flow | 99.7% |
| assets | 99.7% |
| net income | 99.6% |
| equity | 98.2% |
| **revenue** | **86.8%** |

Five sit above 98%. Revenue is the outlier at 86.8% and is also the one that
matters most, which is exactly why it is **not** averaged into a
complete-row requirement: a wide table demanding all six would report the
intersection and hide which concept did the excluding. Its misses stay
visible as its own number.

Dropped from v1: **capex** (79.9%), **shares** (95.1%), **cash** (96.6%),
**operating income** (91.3%). Each returns when a specific question needs
it, carrying its own coverage figure — not as a speculative column that
quietly drags the joint coverage down for every consumer.

**No wide table. Concepts resolve per question.** The measurement is the
argument: individually these clear 98%, together all ten reach 64.2%. A
consumer that asks for revenue and net income should pay the coverage cost of
revenue and net income, not of ten concepts it never reads.

**Absent is a value, not a gap.** 4–10% of operating companies open their
income statement with an expense because they have no revenue. That resolves
to a distinct state — the same distinction as `not_stated` against
`not_parsed`, and the same one as `lapsed` against `declining` in the Form
5500 series. A pre-revenue biotech has no revenue; a filer using an unmapped
tag has revenue we failed to find. Collapsing the two makes the coverage
number a lie in both directions.

**Every concept carries its coverage wherever it is consumed.** A concept at
51% and one at 99% must not look identical downstream, and by default they
do: both are a number in a column. This is the funnel rule applied to a
normalizer, and it is the reason `U10` ships with `tag_map.py` rather than
after it.

**The coverage report ships with the map**, because 11–20% baseline churn
between sampled years means it degrades quietly between now and whenever it
is next read.

#### Source: Financial Statement Data Sets, not companyfacts

Two SEC bulk options, and the choice is not close:

| | Financial Statement Data Sets | `companyfacts.zip` |
|---|---|---|
| shape | quarterly zips, `sub`/`num`/`pre`/`tag` tables | one ~1 GB JSON-per-company archive |
| values | **as filed**, per accession | as *currently* reported |
| restatements | each filing kept separately | overwritten |
| metadata | SIC, fiscal period, form type in `sub.txt` | thin |

**Point-in-time is the whole reason.** `companyfacts` gives today's view of
history, with restatements folded in silently — the same defect as the
yfinance fundamentals rule in CLAUDE.md, which is already stated there and
already cost us a rule. An analog engine that compares a 2018 deal against
2018 fundamentals must use what was *knowable in 2018*. The Data Sets keep
each filing, so they can answer that; `companyfacts` cannot.

~44 quarterly downloads for eleven years, matching the price history. Public
domain, so the parquet goes to **GitHub Releases**, not R2.

#### The core concept set, deliberately small

Ten were proposed. **Six survived the measurement** — revenue, net income,
assets, liabilities, equity, operating cash flow — see the v1 scope below for
why the other four were dropped and what it would take to add them back.

#### Why it is a month

- **Tags are not consistent.** Revenue appears as `Revenues`,
  `RevenueFromContractWithCustomerExcludingAssessedTax`, `SalesRevenueNet`
  and others, varying by filer and by year — ASC 606 moved the whole market
  onto a new tag around 2018, so the map is era-sensitive as well as
  filer-sensitive.
- **Banks, insurers and REITs need separate handling or they silently
  produce garbage.** A bank has no revenue line in the ordinary sense; its
  top line is interest income and its balance sheet does not decompose the
  same way. Branch by SIC, and treat an unbranched financial as unresolved
  rather than as a zero.
- **History mostly starts ~2009.** Anything earlier is not there, and a
  study spanning the boundary needs to say so.
- **The coverage report is the deliverable, not a side effect.** `U10` ships
  *with* `tag_map.py` rather than after it, because a panel showing which
  tags resolved and which fell through is the fastest way to find the next
  branch the map needs. This is the funnel rule applied to a normalizer: a
  concept that resolves for 12% of filers and a concept that resolves for
  98% look identical downstream, and only the count distinguishes them.

#### Order

1. `sources/xbrl/download.py` — the quarterly zips, cached and
   resumable. Named `download`, not `fetch`: the package exports
   `fetch()` as a function and a submodule of the same name shadows it.
2. The measurement above, recorded in this document.
3. `sources/xbrl/tag_map.py` + `resolve.py`, reporting coverage per concept
   as a funnel, with unresolved filers named rather than dropped. **Done
   2026-09-10** — see "v1 as built" below.
4. ~~SIC branches for banks, insurers and REITs~~ — **out of v1.** They are
   a separate table if they are ever built at all; see the v1 scope below.
5. `U10` fundamentals panel, shipping with step 3. **Done 2026-09-10.**

**Exit:** every core concept resolves for a stated share of filers, the
unresolved are listed by SIC with the tags they used instead, and no
downstream consumer reads a fundamental without also being able to read how
many filers it covers.

#### v1 as built — 2026-09-10

`sources/xbrl/` is three modules: `download.py` (quarterly zips, cached and
resumable), `tag_map.py` (the hand-maintained map), `resolve.py` (one quarter
into rows plus the coverage report). `mr xbrl --quarter 2024q1` runs it.

Coverage on 2024q1, 2,804 operating 10-K filers — each figure reproduced by a
test against the real data, so the map's own numbers cannot become folklore:

| concept | resolved | note |
|---|---|---|
| net income | 99.6% | |
| operating cash flow | 99.6% | |
| assets | 99.5% | one tag, the only concept needing no choice |
| equity | 97.7% | |
| revenue | 87.9% | |
| liabilities | 83.2% | stated only; see the rejected derivation above |

Three things the build found that the measurement had not.

**Which tag wins is the definition of the column, not a tiebreak.** 1,280 of
the 2,804 filers report both `NetIncomeLoss` and `ProfitLoss`, and **616 report
different values** — one excludes noncontrolling interests and the other does
not. 781 report two equity tags and 608 of those differ. So the priority order
is a documented decision (parent-attributable throughout, consistently across
net income and equity), and **the resolved tag is carried on every row** so a
consumer can see which definition it was handed. Same lesson as
`TOT_PARTCP_BOY_CNT`: a total and a component under similar names, and it never
errors.

**A nil tag is the strongest evidence of `absent` there is.** A filer that tags
revenue for its own fiscal year and reports no amount has said in the tag that
it has no revenue. The first loader filtered those rows out before resolution,
and 19 clinical-stage biotechs then read as `unmapped` under a tag the map
already carries — a work-queue item that does not exist. The value stays NULL
rather than becoming a zero: a nil tag is not a reported zero, and writing one
in is the same mistake as inferring a split ratio from a price jump.

**"Did not resolve" is five different facts with five different remedies**, so
they are five statuses and never a sum. `absent` needs no work, `unmapped` is
the queue and names the tag, `not_usd` is out of scope, `segment_only` is a
question about aggregating segments, `period_mismatch` is about the filing
rather than the map. The first version applied revenue's evidence — the
income-statement top line — to all six concepts, which produced a liabilities
work queue of 445 filings whose top three suggested fixes were revenue tags:
sorted, plausible, and meaningless. What a filer puts at the top of its income
statement is evidence about revenue and about nothing else.

#### The point-in-time universe is two things, and only one was a purchase

Recorded 2026-09-10, because the two halves wear one name and the project has
been treating them as one problem.

| | status | what it is |
|---|---|---|
| point-in-time **price** universe | **still a purchase** | which symbols had prices on a given date, and the bars for them |
| point-in-time **filer** universe | **free, as of the XBRL load** | which CIKs filed on a given date, under which name, with which SIC |

The filer half is `sources/xbrl/filers.py`: a query over the `sub` tables
already on disk, 1.8 MB a quarter. **11,323 CIKs over 2019–2026**, of which
7,499 are still filing, 3,744 have stopped, and 80 are inside the lag window and
so are `pending` rather than either. Against the current-only `companies` table,
built from SEC's `company_tickers.json`:

| | in the universe | known to `companies` | |
|---|---|---|---|
| still filing | 7,499 | 6,371 | 85.0% |
| **stopped filing** | **3,744** | **102** | **2.7%** |
| pending | 80 | 9 | 11.2% |

A thirty-one-fold gap, and it is shaped exactly like an acquisition: a company
that stopped filing is what a target becomes.

**Be precise about what this fixes, because the two halves look alike.**

*It fixes identification.* Given a deal we can now say who the target was — by
CIK, with a name, an SIC and a filing window — whether or not the company still
exists. Any population selected through `companies` inherits a 97% hole in the
stopped-filing half; selected through the filer universe, it does not. The
practical payoff is a *targeted* sweep: finding the filings of companies that no
longer exist by walking eleven years of daily indexes is ~50,000 requests, and
asking EDGAR about 3,744 known CIKs is 3,744.

*It does not fix survivorship.* A delisted company still has no prices. Nothing
here produces a bar for a symbol Tiingo never carried, so:

- **Deal multiples get more rows.** A multiple is a stated price over a reported
  figure and needs no price history at all, so every target the universe turns up
  is computable.
- **Forward returns stay biased, by exactly as much as before.** They are keyed
  on price history, the delisted targets still have none, and the caveat rendered
  beside every excess figure stands unchanged. Knowing who a company *was* does
  not tell you what its shares did after a deal we have no bars for.

Two different questions, one of which moved. The rule in CLAUDE.md is refined
rather than retracted for that reason.

#### The zips do not need keeping, and the submissions tables do

Asked and answered 2026-09-10. After a 30-quarter load the cache held 4.3 GB of
zips for partitions totalling 8 MB, on a disk with 53 GB free.

What each table is for, per quarter:

| | size | needed for |
|---|---|---|
| `sub.txt` | 1.8 MB | the filer universe, the population counts |
| `pre.txt` | 90 MB | the income-statement top line, during resolution only |
| `num.txt` | 490 MB | the values, during resolution only |
| `tag.txt` | 18 MB | nothing here — labels the map records by hand |

So the steady state is: **keep `sub.txt`, drop the rest, drop the zip.** 54 MB
across the range instead of 4.3 GB, and the filer universe stays rebuildable
from disk with no network at all.

The zip buys one thing: a re-resolve without re-downloading, which matters
because the tag map is hand-maintained and every tag added to it is a reason to
run the range again — that happened twice on the day it was built. But a
re-download is ~30 requests and about twenty minutes against SEC's 10/sec, with
no daily quota in the way. **That is a convenience worth 20 minutes, not 4.3 GB.**

`prune` therefore keeps `sub` by default and takes `drop_zip`, and
`mr xbrl --drop-zips` is the setting to use once a range has loaded clean. One
exception is deliberate: the reference quarter's zip is worth keeping, because
`test_the_map_reproduces_its_own_measurement` re-measures every coverage figure
the map records against it, and that test skipping is worse than 124 MB.

#### The post-606 range, loaded 2026-09-10

30 quarters, 2019q1 through 2026q2, **33,400 operating 10-K filings**. Resumable
and pruned as it goes: 30 quarters unpack to ~18 GB of tab-separated text and the
partitions are 8 MB, so the extracts are deleted after each quarter and the zips
kept — re-resolving is a normal operation here, because every tag added to the
map is a reason to run the range again, and the zip is what keeps that from being
a 3 GB re-download. Peak footprint is one quarter.

**Revenue is stable. It does not degrade going back.** Same-quarter-of-year,
which is the only comparison that means anything:

| q1 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|
| revenue | 91.2% | 88.6% | 87.9% | 85.9% | 85.4% | 87.9% | 87.8% | 87.7% |
| liabilities | 74.4% | 76.8% | 79.8% | 82.3% | 83.2% | 83.2% | 84.3% | 84.6% |

Revenue drifts 3.6 points *down* over eight years and the oldest quarter is the
highest, so the 87.9% measured on 2024q1 is representative rather than a peak.
Liabilities is the one that moves — **+10 points** — because filers increasingly
tag a total liabilities line. Pooled across all 30 quarters: revenue 86.7%, net
income 99.2%, assets 98.9%, liabilities 82.0%, equity 97.6%, operating cash flow
98.9%.

**Quarters are not comparable to each other**, and this is the trap in reading
the series. q1 carries the December fiscal year ends and averages 2,940 filings;
q2–q4 are everyone else — retailers with January year ends, tech with June — at
342–631 filings each, and they resolve revenue about three points lower. The
first version of the span metric compared the oldest loaded quarter to the newest
whatever they were, read 2019q1 against 2026q2, and reported revenue falling
8.1pp. It is not falling; that was the calendar. The same mistake as comparing a
sponsor's 2022 plan set against its 2024 one, one source later.

Two tags were added off the unmapped queue, which is what the queue is for:
`SalesRevenueNet` and `SalesRevenueGoodsNet` were the two biggest entries in
2019q1 and took that quarter from 89.1% to 91.2%. They are the *pre-606* tags,
and they belong in the post-606 map ranked last: the era boundary describes where
the distribution moved, not a date after which an old tag became invalid.

Also corrected on the way through: `tests/test_repo_invariants.py` globbed
`sources/*.py` non-recursively, so a source that is a *package* was invisible to
all three repo rules — no URL check, no freshness check, no ban on
non-deterministic row picks. Nothing failed, which is the problem.

#### Decisions needed before starting

- **Eleven years or fewer.** Matching the price history costs ~44 downloads
  and a few GB of parquet; a 3-year slice is far cheaper and enough to build
  the machinery. Recommendation: build on 2 quarters, then backfill wide.
- **Whether unresolved filers block a release.** Recommendation: no. Ship
  with coverage stated, because a normalizer that refuses to publish until
  it is perfect never publishes.

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
- **Form 5500 entity resolution.** ~~Expect a manual review queue
  permanently.~~ **Measured 2026-09-10 and this was wrong.** EIN is present
  on 100% of filings, so resolution is an exact join and the review queue is
  ~23,000 name-matched-but-EIN-mismatched sponsors rather than 800k names.
  The risk that remains is the opposite one: name matching *looks* like it
  works. Normalized-name precision is 44.2% against EIN ground truth, and a
  matcher wrong more than half the time is worse than none because the errors
  are invisible. Do not add fuzzy matching to the public-match path.
- **Weekend 4 is closed** (2026-09-10). Three plan years loaded, the
  participant series built, the mature-target screen shipped with its funnel.
  It was not a weekend: sponsor resolution turned out to be the easy part and
  the volume was only the second hardest. The hard part was that five
  separate things in the sponsor list were not employers with a headcount --
  see "The screen failure mode" above.
- **`sources/usaspending.py` is deferred, not dropped.** Federal contract
  awards are additive to the private-company work rather than blocking it:
  nothing in Weekend 4 reads them, and the mature-target screen does not
  improve by knowing which sponsors hold contracts until there is a reason to
  ask. Revisit when a sector needs it.

- **A subsidiary of a public company reads as private.** Found in the
  mature-target output on 2026-09-10: `ARCELORMITTAL TUBULAR PRODUCTS USA
  LLC` ranks as a century-old private employer because *its* EIN matches no
  SEC filer — the parent files with the SEC, the subsidiary sponsors the plan.
  The EIN join is doing exactly what it should and the answer is still wrong
  for the question being asked.

  This is not fixable by loosening the join: matching on the name is the
  44.2%-precision mistake, and there is no parent/subsidiary link in Form
  5500. A partial fix exists in EX-21 subsidiary lists attached to 10-Ks,
  which name subsidiaries in text and would give a real link for the filers
  that matter. Until then the category is present and uncounted, so treat a
  recognisable corporate name in the private list as a prompt to check rather
  than as a finding.

- **Mutual benefit societies are nonprofits the NAICS tier misses.** Also
  from that output: `THE BRIDGEPORT FIREMEN'S SICK AND DEATH BENEFIT` (NAICS
  541990) and `FINZER BROTHERS BENEFICIAL ASSOC` (327100) sit in
  for-profit-coded sectors and sponsor no 403(b), so neither tier catches
  them. Small in number and visible by name, which is why the flag is a
  filter rather than a deletion — but it is a known hole in the weak tier
  rather than a surprise.

**Operational:**

Six free tiers means six ways to silently stop returning data while the job
still exits green. Every stage asserts row count and max timestamp. The LLM
router degrades to the next provider rather than killing the run.
