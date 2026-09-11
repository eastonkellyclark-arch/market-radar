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
uv run pytest                       # tests (npm ci first, for the DOM suite)
uv run mr prices --date 2026-09-03  # single source, one date
uv run mr screens                   # rebuild screens from local data
uv run mr digest --dry-run          # render email, don't send
uv run mr backfill --budget 200     # drain N queue items
uv run mr xbrl --quarter 2024q1     # normalize one quarter of fundamentals
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

**Join on the identifier the source guarantees, never on the string humans
typed.** CIK over ticker. EIN over sponsor name. Accession over timestamp.

This has been the answer three times, each time after a name-based join had
already been designed:

- **Tickers get recycled.** 356 active symbols carry two different companies
  inside a ten-year pull, so a ticker is not an entity across time. Prices
  key on `(ticker, listing_id)` and `companies` keys on CIK.
- **Timestamps collide.** Two Form 4s from two insiders at the same company
  filed the same second are not a duplicate -- they are a *cluster*, the
  single signal Weekend 3 exists to detect -- and the original
  `(kind, source, company_id, occurred_at)` key silently rejected the second
  one. Filing signals key on accession, which EDGAR assigns and never reuses.
- **Sponsor names are a coin flip.** Form 5500 normalized-name matching has
  44.2% precision against EIN ground truth: wrong more often than right. EIN
  is on 100% of 1,023,597 filings. Resolution keys on EIN, and there is no
  fuzzy matching on the public-match path.

The failure mode is always the same and it is always invisible: a name-based
join does not error, it silently matches the wrong row, and every number
downstream stays plausible. A matcher that is wrong more than half the time
is worse than no matcher at all.

Names are for *display* and for *review*, never for joining. Where only a
name exists, the answer is a review queue with a human-confirmable record --
not a similarity threshold.

**Every screen ends with a funnel.** The exact parallel of the freshness
assertion on every loader, one step later in the pipeline: `assert_fresh`
refuses to let a job exit green on empty data, and the funnel refuses to let
a screen report a plausible list without saying what it discarded to get
there.

**A short list is either selective or broken, and the surviving count at each
stage is what tells you which.** Five defects in the Form 5500 screen
(2026-09-10) all survived a green suite, because a test encodes what its
author believed about the data and each defect *was* a gap in that belief.
Four were the same mistake -- the list contained something other than what
the screen claimed to measure -- and the broken version consistently looked
*more* plausible than the correct one. The one caught cheaply was caught by a
stage count showing 922,340 -> 122,053 where a few thousand were expected.

`screens/funnel.py` holds the shared type; a test walks `screens/` and fails
the build on one that does not build a funnel. Each stage carries why it
exists next to its count, a stage that empties the population is named, and a
stage that removes more than 90% is marked for reading. Full write-up in
docs/build-spec.md under "The screen failure mode".

**Jobs are idempotent, *and* deterministic.** Re-running yesterday's load
produces the same result, never duplicates. Upsert on natural keys.

Idempotent writes are not enough on their own, and this is a distinction the
codebase learned the hard way. `build_sponsors` picked the sponsor name with
`any_value()`, and three loads of byte-identical input produced 22,680,
22,685 and 22,686 review rows -- because which spelling of a name won decided
whether that sponsor name-matched an SEC filer at all. Every write was a
correct upsert. Every test passed.

So: **no SQL function that picks a row from a group without saying which
row.** `any_value`, `arbitrary`, `first`, `last` are banned in `sources/` and
`screens/`; `min`/`max`/`min_by`/`max_by` on an explicit key are the fix,
because they are a rule rather than a coin flip. A test in `tests/` parses
every module in both packages and fails the build on one, ignoring SQL
comments so the note explaining the ban is not the thing that trips it.

The test that catches this at runtime is a load run *three times* on
identical input with the full output compared -- counts included, not just
row presence.

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
- Node and jsdom are a **test-only** dependency, for running the dashboard's
  inline script in a real DOM rather than asserting on its source text. Nothing
  in `src/` touches node and neither data job does; `tests.yml` installs it
  because `tests/test_dashboard_js.py` fails rather than skips without it

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

**A scratch file a loader reads back must not be a shared path.** `publish`
merged each year partition into a fixed `staging/merged.parquet` and read the
result back from it, so on a fast POSIX runner every partition after the first
published and asserted the *previous* year's rows — 2024 got 2023. It passed on
Windows and in every local run for weeks, and failed the first time the suite
ran on an Actions runner. The fix is not a flush or a retry: the path is shared
mutable state between iterations, so each call gets a scratch directory of its
own, which is what `selftest.py` and `sec_company_tickers.py` already did.

Write scratch to a per-call temp dir, clean it up, and keep a test that the
staging directory is empty of everything but its chunks afterwards — a shared
path is visible as the file it leaves behind, on any platform, which is the
only portable form this check has.

**Volatility screens run in three price bands** — sub-$1, $1–10, $10+ — kept
separate. One combined list means penny stocks win every day and you never see
a $40 stock move again. Store tick-count move alongside percent; $0.0002 →
$0.0003 is +50% and one tick.

**XBRL tags are not consistent, and it is one cliff rather than a mess.**
Measured 2026-09-10 across seven quarters, 2013q1 to 2024q1. Revenue appears
as `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`,
`SalesRevenueNet` and others, but the variance is almost entirely an *era*
effect: 78.2% of filers changed their income-statement top-line tag between
2018 and 2019 when ASC 606 landed, against 11-20% in every other sampled
interval. `SalesRevenue*` went from 48.4% of filers to 2.8% in that one year.

So the map is era-keyed with **exactly one boundary** (fiscal years beginning
on or after 2017-12-15), and the two halves are not equal work: pre-606 has
419 distinct top-line tags with the top 5 covering 71.9%, post-606 has 133
with the top 5 covering 93.0%. Build post-606 first; pre-2019 is a separate
decision on evidence.

**When two XBRL tags carry "the same" concept, the choice between them is
the column's definition.** 1,280 of 2,804 operating filers in 2024q1 report
both `NetIncomeLoss` and `ProfitLoss`, and 616 report *different values* — one
excludes noncontrolling interests, one does not. 781 report two equity tags and
608 of those differ. So tag priority is a documented decision, never a
tiebreak, and the resolved tag rides on every row so a consumer can see which
definition it got. Third time this shape has appeared: `TOT_PARTCP_BOY_CNT`
over active participants, the Form 5500 headcount sum over the largest plan,
and now this. A source offering a total and a component under similar names is
the default case, not the exception.

**A nil XBRL tag is evidence, and it is not a zero.** A filer that tags revenue
for its own fiscal year and reports no amount has stated that it has no
revenue — stronger evidence of `absent` than anything inferable from the
statement layout. Keep those rows and read them; do not write a 0 in their
place, for the same reason a missing split is never inferred from a price jump.

**Banks, insurers, brokers and REITs are not a SIC branch — they are a
different table.** 23.7% of filers in 2024q1 and 26.2% in 2013q1. A bank's
top line is interest income and a REIT's earnings measure is FFO; forcing
either into an operating-company shape produces numbers that are present,
plausible and wrong.

**Concepts resolve independently, and requiring a complete row is a silent
filter.** Individually most concepts clear 90%+ for operating companies, but
all ten on the same filer is 64.2% in 2024 and 51.6% in 2013. Never build one
wide table and join against it: a consumer asking for two concepts should pay
the coverage cost of two, not of ten it never reads. **Every concept carries
its own coverage number wherever it is consumed** — a concept at 51% and one
at 99% are both just a number in a column otherwise.

**v1 is deliberately narrow** (decided 2026-09-10): post-606 only (2019
forward), operating companies only, and six concepts — revenue, net income,
assets, liabilities, equity, operating cash flow. Five are above 98% in 2024;
revenue is 86.8% and is the one that matters, so its misses stay visible
rather than averaged into a complete-row requirement. capex, shares, cash and
operating income come back when a question needs them, each with its own
coverage stated.

**A company with no revenue is `absent`, not `unmapped`.** 4-10% of operating
companies open their income statement with an expense because they are
pre-revenue. That is a distinct value -- the same distinction as `not_stated`
against `not_parsed`, and as `lapsed` against `declining` in the Form 5500
series. Collapsing them makes the coverage number wrong in both directions.
History mostly starts ~2009. Full numbers in docs/build-spec.md.

**M&A detection is by SEC form type, not news.** 8-K Items 1.01/2.01, S-4,
DEFM14A, SC 13D, SC TO-T, SC 13E-3. Filings are legally required, timestamped,
and unambiguous. News is the noisy secondary signal.

Measured 2026-09-10 and **news was declined**, so this is settled rather than aspirational. GDELT returns 14.3 articles per company per day and **13.4% of them name the company in the headline**; the rest are passing mentions, content farms and the company's own portal. The deciding test was lead time against real 8-K deal dates: coverage did appear before the filing, but not one leading article was about the deal -- they were insider-transaction reports we already parse from Form 4, unrelated PR, and stock-performance filler. Its API also cannot be swept (one request per five seconds, tighter under load), and the free bulk GKG fixes the rate limit without fixing the base rate. Full numbers in docs/build-spec.md under "News: measured, declined". Reopen on lead time, not on volume.

**"Absent from the file" is not "declined to zero", and only one of them
is a signal.** Measured 2026-09-10 while building the participant time
series. A sponsor missing from a later plan year may have terminated the
plan, been acquired, changed EIN, dropped under the filing threshold, or --
overwhelmingly, in the newest year -- simply not filed yet. Filings lag the
plan year by about eighteen months, so 2025 held roughly a third of 2024's
filings while it was still being filed.

Folding an absence into the participant series as a zero manufactures a
cliff for a large share of the file, and a screen looking for shrinking
headcount sorts exactly those to the top -- it would rank its own blind spot
first. So presence and trend are **separate columns**: `status` is
`filing`/`lapsed` and never carries a direction, `trend` is measured only
between plan years the sponsor actually filed and only complete ones, and
`pending_years` (still being filed, means nothing) is counted apart from
`gap_years` (a complete year skipped, a real oddity). `unknown` is a real
trend value and the most common one.

The same shape recurs wherever a source has a reporting lag: a row that is
not there yet and a row that is there and small are different facts, and
the arithmetic that treats them alike never errors.

**And it recurs one level down, at whatever the row is made of.** The first
build of the participant series keyed correctly on the sponsor and then
compared an aggregate whose *membership* drifts: a sponsor's set of filed
plans changes year to year, so "everything it filed in 2022" against
"everything it filed in 2024" is two different things with the difference
called headcount. Edward Don & Company filed two plans for 2022 and one for
2024 and read as −46%. The comparison now runs over the plans present at
both ends, keyed on `(ein, plan_num)`. Before comparing two aggregates
across time, ask what the set is made of and whether the membership is the
same at both ends.

**Check what the column counts before naming it.** `TOT_PARTCP_BOY_CNT` is
the obvious participant field on Form 5500 and it is not headcount: it
includes retirees and separated ex-employees who still hold a balance.
Active is 78% of total on the main form, and for an old institution far
less — Boca Raton Regional Hospital reports 934 participants and 388 active.
A trend on the total finds pension plans distributing balances, which every
old employer does, and the first mature-target list was hospitals,
universities and charities doing exactly that.
`TOT_ACT_PARTCP_BOY_CNT`/`SF_TOT_ACT_PARTCP_BOY_CNT` is the one that means
employees. The general rule: when a source offers a total and a component,
read the field definition rather than the field name — the total is usually
a superset of what you want, and it never errors.

**Some numbers are floors, and a floor is not a measurement.** Form 5500's
`PLAN_EFF_DATE` gives the oldest plan a sponsor still files, which bounds
how long the company has existed without being its age -- a firm founded in
1971 whose 401(k) started in 1985 reads as 1985. The error runs one way
only: it understates, which hides targets rather than inventing them. That
asymmetry is what makes it usable as a screen input and unusable as a
reported fact, so it renders as `≥N years` and never as `N years`. Same
rule as the Form 5500 headcount *range* (sum across plans is a ceiling,
largest single plan a floor) and the survivor-only price universe: state
the direction of the error wherever the number is shown.

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

**`corporate_actions` coverage has two separate causes, and only one was
ours.** Diagnosed 2026-09-09 after the table was found holding 365 splits
across eleven years.

*Cause one, fixed.* `upsert_corporate_actions` issued one round trip per row
and then returned `len(rows)` regardless of outcome. A 10-year backfill
stages about 254,000 actions; that many sequential round trips do not finish,
and the function reported complete success having written a tenth of them.
The staged parquet held 3,724 splits and Postgres held 365. It now inserts in
batches of 500 and **verifies by anti-join against the staged set, raising if
anything is missing** — the count it returns is what landed, not what it
attempted. Backfilled from staging: 253,645 rows, 3,720 splits, 9,156
tickers.

*Cause two, not fixable at this plan.* Tiingo's per-bar `splitFactor` on
`/prices` is itself incomplete, and worst on exactly the small tickers where
reverse splits are constant. POWW, HSCSW and CAPS each report **zero**
splits across their whole history while their raw closes jump 78x, 100x and
1115x; SBFM reports four and misses a fifth. There is no separate
corporate-actions endpoint to fall back on — `/tiingo/corporate-actions/...`
returns 403 on Power, and fundamentals is DOW-30 only. Closing this gap
means a different data plan, so **do not assume the action table is
complete**, and do not "fix" a missing split by inferring the ratio from the
price jump and writing it in: an inferred action is fabricated data in a
table the screens trust.

What exists instead is `screens/action_audit.py`, which needs no knowledge of
the loader: a large one-session move with no action in the interval to
explain it is a candidate missing split, whatever the cause. The digest
health block reports the count over the last 7 sessions, which is the check
that would have caught cause one on the first night. The residual backlog is
about 3,300 unexplained jumps and 1,200 falls across 1,600 tickers.

An unexplained jump is the dangerous direction: unadjusted, a reverse split
reads as an enormous *gain* and tops a gainer list. PHD went 0.40 to 9.95 on
2026-09-03 — a 1-for-25 reverse split with no action on record, worth
+2,388% in the screens.

Exchange test symbols were leaking into the price history for the same
reason nobody noticed the splits: nothing checked. `ZBZX` alone produced 26
"unexplained moves" and `PTEST-Z` printed a 0.05 to 25.00 jump, because a
test symbol's quote is arbitrary by design. `TEST_SYMBOL` now covers the
NASDAQ, NYSE, Cboe and IEX families. Already-published partitions still carry
them until a `--restate` sweep.

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
