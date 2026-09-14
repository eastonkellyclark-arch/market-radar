# Operating Market Radar

One page. What runs without you, what you run, what to look at each morning, and
what to do when something is red.

---

## What runs unattended

Two workflows in the cloud, chained, plus one scheduled task on this machine.

| | when | what it does |
|---|---|---|
| **`prices`** | `30 3 * * 2-6` — 03:30 UTC, Tue–Sat | Sweeps the whole market from Tiingo (~12k requests, ~95 min), publishes the year partition to R2, writes `dataset_stats`. |
| **`digest`** | when `prices` **succeeds** | `mr fred`, renders and sends the email, then `mr manifest --verify`. |
| **`Market Radar decks`** | daily 01:30 local, **local scheduled task** | `mr decks --promoted`: renders a deck for every name three sentinels promoted and gated, prunes runs past the newest 30. Logs to `.decks/run.log`. |

**Why the deck job is local and not a third workflow.** A deck's price page is
raw Tiingo OHLCV and its fundamentals page is XBRL, so the file is vendor data.
Release assets on a public repo are downloadable, which would make publishing one
redistribution; and a static dashboard opened from `file://` cannot read the
private R2 bucket either. There is nowhere in the cloud for a deck to go and
still be useful, so it is written next to the dashboard that links it. Same
reason yfinance never runs in an Action, arrived at from the licence rather than
from the rate limit.

01:30 local is chosen against the sweep, not against the clock: `prices` starts
03:30 UTC and takes about 95 minutes, so the partition republishes around 00:05
US-Central. Running earlier would promote *yesterday's* session and look
completely normal doing it. Register it with:

```
schtasks /create /tn "Market Radar decks" /tr "%CD%\scripts\run_decks.cmd" /sc daily /st 01:30 /f
```

The machine has to be awake. For a laptop that sleeps,
`schtasks /change /tn "Market Radar decks" /ri 60 /du 08:00` retries hourly
through the morning instead.

Two things about that chain are deliberate and worth knowing, because they look
like bugs otherwise:

- **The digest has no clock of its own.** It fires on the sweep *finishing*. A
  fixed time would have rendered mid-sweep — the schedule once fired 4h47m late,
  which would have produced a digest of half the market that looked entirely
  normal.
- **A red sweep sends no digest.** `if: workflow_run.conclusion == 'success'`. A
  failed sweep leaves a partition mid-rewrite, and a digest of that is worse than
  no digest because it reads like an ordinary morning. **So no email is itself a
  signal** — see below.

`tests` runs on push. It needs `npm ci` for the jsdom suite; `tests/test_dashboard_js.py`
fails rather than skips without node.

---

## What you run by hand

**The daily one, if any:**

```bash
uv run mr dashboard          # build and open the local dashboard
```

That is the main way to look at the system. Everything else is occasional.

It takes about **five minutes** and writes a **~5.8 MB** file, because the ticker
chart reads all eleven price partitions rather than the two a screen needs — a 5Y
button on two years of data would be a lie. Candle timeframes are 1D through All,
default 3M, and the selection follows you from one ticker to the next. At 5Y and All
the candles are weekly and monthly **aggregates** (open of the first session, close
of the last, max high, min low, summed volume) and the chart says which; `log` is
there for the sub-$1 names, where a 0.0751-to-0.98 range otherwise reads flat.

**Enrichment that must stay local.** yfinance is rate-limited by IP and Actions
runners sit on datacenter ranges. Any job importing it is unreachable from
`.github/workflows/` on purpose, and the market-cap step for the Tier 2 shortlist
(15–40 names/day) is a local, hand-run step. Run it from home, not from a VPS.

**Occasional, in rough order of how often you'd want them:**

```bash
uv run mr screens                    # rebuild screens from local data
uv run mr outcomes                   # forward returns (pure SQL, no LLM)
uv run mr actions-audit              # large moves no corporate action explains
uv run mr deals                      # 8-K Items 1.01/2.01
uv run mr form4                      # Form 4 purchase clusters
uv run mr comps                      # peer sets
uv run mr dcf                        # valuations, substitutions on every row
uv run mr xbrl --quarter 2024q1      # normalize one quarter of fundamentals
uv run mr form5500                   # DOL plan data, resolved by EIN
uv run mr symbols                    # recover tickers for stopped filers
uv run mr backfill --budget 200      # drain N queue items, low priority
uv run mr decks --cik 0000066740     # one pitch deck, on demand
uv run mr decks --promoted           # today's Tier 2 set (the scheduled task's job)
```

**Decks are automated on *promotion*, and there is still no `--all`.** 2,569
valuations is 2,569 files nobody opens. What runs nightly is the set three
sentinels promoted — a volatility screen list, a qualifying deal filing, or a
Form 4 cluster — gated on having a valuation to draw. Measured 2026-09-11: **186
promoted, 27 with a valuation, 159 reported rather than rendered.** 72 seconds
end to end, 58 KB a deck.

The 159 matter more than the 27. A filer with no valuation renders ten pages of
"no valuation", "no peer set" and an empty chart, and a deck is the easiest thing
here to mistake for an authoritative one — so the gate prints the count and
writes nothing. Run it by hand any time; `--date` picks an older session,
`--keep-runs` changes the retention, `--form4-window` changes the cluster
lookback.

`--archetype clean growth_mismatch heavy` still picks a filer by evidence quality
instead of making you hunt a CIK, and `--cik` still renders anything at all —
promoted or not.

**The `deck` control in the DCF and deck panels is two controls.** A filer with a
rendered deck gets a **link** to the file; everything else gets a button that
**copies the command**. Which one you see is a fact about the disk, read as the
page is written — never a stored index, because the prune deletes runs and an
index would go on offering links to files it removed. Nothing in the dashboard
generates: it is a static file with no server.

**Rule for all of them: a long sweep resumes by default.** `--restart` is always
explicit. If one dies at request 9,000, run it again — it picks up from the
checkpoint. `mr symbols` prints `N of M chunks done` so you can see that it did.

**Before sending anything:** `uv run mr digest --dry-run` renders without sending.

---

## Each morning, in order

1. **Did the digest arrive?** If yes, read the **health block at the top** — it is
   first precisely so a degraded run cannot look like a healthy one. If no email,
   the sweep failed: go to the next step.
2. **Check the `prices` run** on the Actions tab. Red sweep → no digest, by design.
3. **In the health block, the number to watch is unexplained moves** over the last
   7 sessions. A jump in it means missing corporate actions, not a market event.
   The residual backlog is ~3,300 unexplained jumps and ~1,200 falls across ~1,600
   tickers, so a change matters more than the level.
4. **`uv run mr dashboard`** if you want to look at anything in detail. The
   deck panel's first line says how many filers have a rendered deck and **what
   the newest automated run was dated** — that date is the check that separates
   a quiet night from a generator that stopped. Rows whose filer has a deck link
   straight to it.

That's it. Most mornings are steps 1 and 3.

---

## When something is red

**First, the rule that makes all of this readable: a job that exits green on empty
data is the failure this system is most defended against.** Every loader ends in an
explicit `assert_fresh(...)` that raises. So a red job usually means the assertion
did its job, and the fix is upstream of the code.

| symptom | what it means | what to do |
|---|---|---|
| **No digest, `prices` red** | Sweep failed mid-market. Partition may be half-written. | Re-run the workflow. It resumes from the checkpoint; it will not re-fetch the first 9,000. |
| **`StaleDataError`** | Row count or max timestamp missed expectation. | Believe it. Check the source actually published — do not widen the threshold to make it pass. |
| **`mr manifest --verify` non-zero** | A declared location does not resolve. The nightly runs this after publishing, so it catches drift as well as a bad write. | Check the manifest entry against where the file actually is. |
| **429 from Groq asking for a long `retry-after`** | The **per-day** token cap (200k per model) is spent, not the per-minute one. `x-ratelimit-remaining-tokens` will still read healthy — it is lying. | Stop for the day. A 429 asking for twelve minutes is a closed door, not a queue. |
| **A provider is down** | The router falls through to the next one. | Nothing, unless the job needed a *specific* model — capability-dependent jobs pin their model, because a weaker model fails as a plausible absence rather than an error. |
| **A screen returns a suspiciously short list** | Could be selective, could be broken. | Read its **funnel**. Every screen prints the surviving count per stage and marks any stage that removed >90%. A short list with a plausible funnel is selective; one that collapses somewhere unexpected is broken. |
| **Tests red after a data change** | Often a real invariant. | Run the single test, read its docstring — most record what was broken to prove them. |

**Two sanity commands:**

```bash
uv run mr selftest                   # end-to-end with synthetic data
uv run mr manifest --verify          # does every declared location resolve
```

`mr selftest --inject-staleness` deliberately publishes stale data and **must** exit
non-zero. If it exits zero, the freshness assertions are not working and nothing
else here can be trusted.

---

## Things that will look wrong and are not

- **`unknown` is the most common participant trend.** Absence from a later Form 5500
  year is not a decline — filings lag the plan year ~18 months.
- **Deal multiples is 43 rows.** That stage is *meant* to collapse: most 8-K deal
  filers go on existing, because they are the acquirer or a divesting parent.
- **Outcome figures carry a population note.** They are measured on acquirers and
  collapsed deals, not targets, and biased low. That is rendered next to the
  numbers rather than footnoted, and it is not optional — a test fails the build if
  a renderer drops it.
- **Ages render as `≥N years`.** Form 5500's `PLAN_EFF_DATE` is a floor, not an age.
- **FRED data never leaves the machine.** `macro_series` in Postgres is its only
  home, by licence. There is deliberately no flag to relax that.
- **Most DCF rows offer a command, not a link.** The nightly run renders 27 of
  2,569 valuations on purpose. A row with no file is not a filer that cannot have
  a deck; it is one nothing promoted.
- **The Form 4 leg is usually the smallest, often zero.** `mr form4` is hand-run,
  so the leg is only as fresh as the last time you ran it — and a cluster's
  stored date is its first *purchase*, which precedes the filing that revealed it
  by 3 days at the median. The 7-day window is that lag's p90, and the run says
  so.
- **Decks and `.decks/` are gitignored.** A deck carries Tiingo prices and XBRL,
  so it is vendor data and the repo is public. Six were committed in `3083eb8`
  before this was noticed; they are out of `HEAD` and still in the history.
