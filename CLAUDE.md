# Market Radar

Personal research tool. Public equities intelligence + private company deal
sourcing. Information gathering only — this system never sends outreach.

Single user (Easton). Not a product, not multi-tenant, no auth layer. Optimize
for reliability and low cost, not scale.

Running cost target: **$30/month** (Tiingo Power). If a change adds recurring
cost, say so before writing it.

---

## Architecture

Three tiers. Each narrows the funnel before expensive work runs.

- **Tier 0** — whole market nightly. Bulk EOD prices, screens as SQL. ~Free.
- **Tier 1** — sentinels. EDGAR form-type filters, GDELT news, bulk private
  company data. Deterministic, almost no LLM.
- **Tier 2** — deep dive on 15–40 promoted names/day. XBRL, DCF, embeddings,
  decks. This is the only tier allowed to be expensive.

Full data source inventory and build order live in `docs/build-spec.md`.

---

## Layout

```
market-radar/
├── .github/workflows/     # scheduled jobs — the only orchestration
├── src/marketradar/
│   ├── manifest.py        # dataset location registry — read this first
│   ├── storage.py         # DuckDB + Parquet + Supabase access
│   ├── sources/           # one module per data source
│   ├── screens/           # volatility + fundamental screens
│   ├── signals/           # EDGAR, news, sentinel logic
│   ├── entities/          # CIK/ticker/name resolution
│   ├── llm/               # provider router + prompt templates
│   └── digest.py          # daily email output
├── manifest.toml          # dataset locations (config, in git)
├── sql/                   # schema migrations, screen queries
├── tests/
└── docs/build-spec.md
```

`manifest.toml` holds *where* datasets live — config, changes rarely, belongs
in git where it is diffable. The `dataset_stats` table in Postgres holds *what
is in them* — row counts and max dates, written nightly, read by the freshness
assertions. `manifest.py` is the only way either is accessed.

One module per source in `sources/`. Each exposes `fetch()` and `load()` and
knows nothing about any other source.

---

## Commands

```bash
uv sync                             # install
uv run pytest                       # tests
uv run mr prices --date 2026-09-03  # single source, one date
uv run mr screens                   # rebuild screens from local data
uv run mr digest --dry-run          # render email, don't send
uv run mr backfill --budget 200     # drain N queue items
```

Every job runs standalone from CLI. If it only works inside a GitHub Action,
it's built wrong.

---

## Hard rules

**Never hardcode a data URL.** Always resolve through `manifest.get(dataset,
partition)`. The whole point is that datasets move between GitHub Releases, R2,
and Supabase without touching code.

**The repo is public.** Free Actions minutes depend on it. Never commit a key,
token, endpoint with credentials, or `.env`. Secrets come from Actions secrets
or local env only. If you're unsure whether something is sensitive, ask.

**Vendor data goes to R2. Government data goes to GitHub Releases.** This is a
licensing boundary, not a capacity one, and it is not obvious — so it is
stated as a rule. Release assets on a public repo are publicly downloadable,
which makes publishing them redistribution. Tiingo and Stooq personal terms
forbid that; SEC, DOL, FRED, and USAspending data are public domain and fine.
Anything a vendor touched — prices, corporate actions, vendor news — goes to
the private R2 bucket. The manifest's `backend` column carries the
distinction. When adding a dataset, decide which side it falls on first.

**"From a government source" is not the same test as "ours to republish."**
FRED is the case that separates them, and it is the reason this paragraph
exists. `DGS10` is Treasury data and public domain, but the ICE BofA series
(`BAMLH0A0HYM2`, `BAMLC0A0CM`) are third-party indices that FRED
redistributes *under permission from ICE Data Indices, LLC*. Fetching them is
fine; mirroring them to a world-readable Release asset is redistributing
someone else's index. So the government-source whitelist above is a rule about
the *publisher*, not about every series that publisher carries — check the
series, not just the agency.

Decided 2026-09-08: **FRED is local-only.** `macro_series` in Postgres is its
only home and `sources/fred.py` has no publish path. Not even DGS10 is
published: splitting one API call across two destinations by licence buys
nothing and would leave a publish path that a later edit could widen back over
the ICE series by accident. There is deliberately no flag to relax this,
because a flag is the thing that gets forgotten.

**Never commit data files.** Parquet goes to R2 or GitHub Releases via the
manifest, per the rule above. Repo holds code and SQL only. The one exception
is a small, deliberately chosen set of parser test fixtures — never a bulk
archive.

**Every pipeline stage ends with a freshness assertion.** Row count and max
timestamp against expectation, and it must raise — not warn, not log. A job
that exits green on empty data is the failure mode we care most about. This has
bitten this codebase's predecessor before.

Write it as an **explicit call** to `assert_fresh(...)` as the last statement
of `load()` — not a decorator (it can only see return values, and per-dataset
thresholds make the arguments unreadable) and not a base class (it would
couple every source to a shared parent, which the one-module-per-source rule
forbids). Explicit calls are greppable, visible in every diff, and need no
mocking. A test in `tests/` walks every module in `sources/` and fails the
build if one doesn't call it — that is the enforcement a base class would
give, without the hierarchy.

**Jobs are idempotent.** Re-running yesterday's load produces the same result,
never duplicates. Upsert on natural keys.

**SEC requests** need a descriptive User-Agent with a real contact email, and
must stay under 10 req/sec. Prefer bulk zips (`companyfacts.zip`, Financial
Statement Data Sets) over per-CIK loops — one download beats 8,000 calls.

**Tiingo is the source of record for prices.** Whole-market sweeps run against
it — ~12k requests covers every US ticker, well inside the published 100k/day
and 10k/hour caps. (The former rule sent whole-market sweeps to Stooq bulk;
that was written against stale pricing and a bulk file that has been behind a
CAPTCHA since 2020-12-10. Stooq is now only a parser test fixture and an
independent cross-check.)

**Sweeps are chunked, checkpointed, and resumable.** A 12k-request pass takes
over an hour and will be interrupted. Write a checkpoint after every chunk;
resume from it by default on re-invocation; make `--restart` an explicit flag.
A failure at request 9,000 must not discard the first 9,000, and a half-swept
market must never be published to the manifest.

**yfinance is never called from a GitHub Action.** It runs locally, from a
residential IP, only. Yahoo rate-limits by IP and Actions runners sit on
datacenter ranges that get flagged quickly — 40 requests from home is safe,
12k from a cloud IP is not. The split is by execution environment, not by
capability: Tiingo is the scheduled cloud path and the source of record;
yfinance is a local, hand-run enrichment step over the Tier 2 shortlist
(15–40 names/day: market cap, EV, trailing/forward P/E, EV/EBITDA, shares
outstanding). Any job that imports yfinance must be unreachable from
`.github/workflows/`.

**yfinance fundamentals are current values, not point-in-time.** They describe
the company as of today, with no as-of date and no restatement history. That
is fine for screening today's names and **wrong** for anything historical. The
analog engine — comps, forward returns, "similar deals" — must use XBRL joined
to our own price history, never yfinance.

**LLM calls route by difficulty** through `llm/router.py`. Classification and
extraction go to local Ollama or Groq. The good model is only for decks and
narrative analysis. Never call the good model inside a loop over the market.

**Provider failure degrades, never crashes.** The router falls through to the
next provider. Six free tiers means six things that can change without notice.

---

## Conventions

- Python 3.12, `uv`, type hints on public functions
- DuckDB for analytical queries over Parquet; Supabase Postgres for entities,
  signals, queue, manifest, embeddings
- Prices as `DECIMAL(18,6)` in Parquet and `Decimal` in Python — never float,
  and **never integer cents**. Cents cannot represent $0.0002, and the sub-$1
  band is a headline feature. Other money values may use `Decimal` or integer
  cents where sub-cent precision is genuinely impossible.
- All timestamps UTC in storage; convert only at the digest layer
- Dates as `date` objects, never strings, outside of I/O boundaries
- Embeddings: 256 dimensions, stored as `halfvec`
- New dependencies need a reason — Actions install time is real

---

## Gotchas

**Store raw prices, adjust at query time.** Percent moves must always use
adjusted prices — reverse splits are constant in the sub-$1 band and read as
−95% days otherwise, the single biggest source of fake signals in this system.
But do not *store* adjusted prices: adjusted history is retroactively
rewritten by every split, so a nightly job that only touches the current-year
partition would let older partitions silently drift from the source. Parquet
holds raw OHLCV; `corporate_actions` holds `split_factor` and `div_cash`; the
cumulative factor is applied in the query. History stays immutable and
append-only, and a bad adjustment is a fixable bug rather than a re-download.

**Volatility screens run in three price bands** — sub-$1, $1–10, $10+ — kept
separate. One combined list means penny stocks win every day and you never see
a $40 stock move again. Store tick-count move alongside percent; $0.0002 →
$0.0003 is +50% and one tick.

**XBRL tags are not consistent.** Revenue appears as `Revenues`,
`RevenueFromContractWithCustomerExcludingAssessedTax`, `SalesRevenueNet`, and
others depending on filer and year. The mapping lives in
`sources/xbrl/tag_map.py` and is maintained by hand. Branch by SIC code —
banks, insurers, and REITs need separate handling or they silently produce
garbage. History mostly starts ~2009.

**M&A detection is by SEC form type, not news.** 8-K Items 1.01/2.01, S-4,
DEFM14A, SC 13D, SC TO-T, SC 13E-3. Filings are legally required, timestamped,
and unambiguous. News is the noisy secondary signal.

**Form 5500 sponsor names are messy.** DBAs, legal entity names, and subsidiary
rollups all differ from how a company is known. Fuzzy match into a review
queue; never auto-merge entities above a similarity threshold without a
human-confirmable record.

**The price universe is survivor-only, and that breaks deal studies in the
one place it matters.** Tiingo's supported-ticker list is *current* listings,
so a company that was acquired is not in the history at all — not truncated,
absent. Checked directly: LNKD, WFM, TWTR, ATVI and VMW all return zero bars.
An acquisition target is by definition a company that stopped being listed,
so any forward-return study keyed on tickers measures **acquirers and failed
deals, never takeout premiums**. The 8-K population shows the same curve from
the other side: 3,406 joinable Item 1.01/2.01 filings in 2016 against 8,101
in 2025, while total filings per year stayed flat near 12,000 — the
difference is ten years of delisting, not ten years of growth.

Do not fix this by widening the ticker map. SEC's `company_tickers.json` is
also current-only, and even a perfect historical CIK→ticker map would resolve
to symbols we hold no prices for. It needs a point-in-time universe, which is
a data purchase, not a query. Until then: state the bias whenever a study
reports an outcome, and never describe such a result as "returns after a
deal" when it is "returns after a deal, among companies that survived it".

Watch for the second-order version too. A recycled ticker makes a dead
company look alive — SGEN carries bars through 2026 because a different
issuer took the symbol after Seagen was acquired in 2023. The 30-day anchor
guard in `screens/outcomes.py` rejects those, the same way the gap guard does
in the volatility screens.

**`corporate_actions` is materially incomplete, and nothing warns you.** It
holds 365 splits across eleven years and 2,947 tickers, which is far short of
reality. AYTU's 1-for-20 reverse split of 2023-01-06 is simply absent, and
its absence reads as a genuine +1,751% move. Thirteen such events moved one
study's mean excess return from +5% to +944% while its median stayed at +5%.

This is the failure mode the "adjust at query time" rule was written to
prevent, arriving through the back door: the adjustment is applied correctly
to an action table that does not contain the action. The volatility screens
read the same table, so this is not confined to the outcome study.
`screens/outcomes.py` flags a move past 300% with no action on record as
`suspect_unadjusted` and counts it in the open rather than dropping it
silently — the same band holds real takeouts. Backfilling the actions
properly is still owed.

**Most "similar historical deals" is SQL, not vectors.** Filter first on SIC
code, deal size bucket, cash vs stock, era. Use embeddings only to rank within
that result set. Forward-return outcomes are a pure SQL join against local
price history — zero LLM calls.

**Backfill drains leftover quota**, after the day's work, at low priority.
It never blocks current-day processing. Clicking into a company jumps its
history to the front of the queue.

---

## Out of scope

Do not build, and flag if asked:

- Any outreach — email, SMS, CRM sync, contact enrichment. Information only.
- Multi-user features, auth, billing, public API.
- Anything that redistributes vendor data. Free tiers here are licensed for
  personal use; sharing outputs containing their data breaks that.
- Trade execution or broker integration.

---

## Working style

- Ask before adding a data source, a dependency, or a recurring cost.
- Prefer bulk downloads over scraping. If a scraper is unavoidable, check for
  a bulk file first and say why it wasn't usable.
- When a free tier's documented limits matter to a design choice, verify them
  rather than assuming — they change often.
- Small commits, one source or one screen at a time.
