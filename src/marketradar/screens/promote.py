"""Today's Tier 2 population: the names three sentinels agree are worth the money.

Tier 2 is the only tier allowed to be expensive, and the architecture sizes it at
15-40 names a day. Nothing in this codebase computed that set. `mr decks` took a
CIK or an archetype, which is fine for looking at one filer and no basis at all
for a nightly job -- and a nightly job over the whole valued population would be
2,569 files nobody opens.

Three legs, deliberately different in kind:

    volatility      a name in any of the day's 24 screen lists
    deal_filing     an 8-K the deals loader classified as a real deal
    form4_cluster   two or more insiders buying the same issuer

**Every leg is keyed on CIK and none of them starts there.** The volatility screen
knows tickers, the cluster loader stores a composite key, and only `deals` carries
a CIK column. So resolution is the first thing that happens and the funnel's second
stage is what it cost, because a leg that silently resolves nothing is a leg that
looks like a quiet day.

Measured 2026-09-13 against the session of 2026-09-11:

    volatility        345 tickers across the 24 lists
                      208 of them stock rather than ETF
                      178 distinct CIKs
    deal_filing         8 filed, 7 of a promoting type
    form4_cluster       1 inside the 7-day window, 0 stamped that day
    union             186 filers

**186 is not 15-40, and the gap is the whole reason :func:`gate` exists.** Of
those 186, **27 carry a DCF valuation (15%)** and the other 159 would render ten
pages of "no valuation", "no peer set", "-- --" and an empty candle chart. A deck
is the easiest artifact in this system to mistake for an authoritative one; a
directory of 159 hollow ones is worse than no directory. So the gate is not a
performance optimisation, it is the same discipline the deck's own provenance
footer carries, applied one level up: **report the count, do not emit the file.**

By leg, on that session: volatility 25 of 178 (14%), deals 2 of 7 (29%). The
volatility leg is low for a reason that is not a defect -- a one-day mover list is
mostly sub-$1 names and ETFs, and neither has an income statement to discount.

**The Form 4 window, and why it is seven days.** `signals.occurred_at` for a
cluster is `Cluster.first` -- the date of the earliest *purchase* in it, not the
date we could see it. Form 4s are filed up to two business days after the trade
and a cluster spans several of them, so the lag from `first` to visible is, over
19,127 historical clusters, a median of 3 days and a p90 of 7. A job asking
"clusters whose occurred_at is today" would therefore find almost nothing on
almost every day, which is exactly what it did: **0 clusters stamped 2026-09-11
with 28 in the table**, against 1 inside the seven-day window. So the leg takes a window, it defaults to the measured p90, and
:data:`FORM4_WINDOW_DAYS` says what share that covers. The remaining decile is a
late filing and is stated rather than closed -- the alternative is storing a
visibility date the loader does not currently write.

The same shape as the Form 5500 lag: a row that is not there *yet* and a row that
is there and small are different facts, and the arithmetic that treats them alike
never errors.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Final, Iterable

from marketradar.entities.cik import cik_key, cik_sql
from marketradar.screens import funnel as funnel_mod

log = logging.getLogger(__name__)

SCREEN: Final[str] = "promote"

#: The three reasons a name reaches Tier 2, in the order they are reported.
VOLATILITY: Final[str] = "volatility"
DEAL_FILING: Final[str] = "deal_filing"
FORM4_CLUSTER: Final[str] = "form4_cluster"
REASONS: Final[tuple[str, ...]] = (VOLATILITY, DEAL_FILING, FORM4_CLUSTER)

#: Deal types that promote. `spac` and `securitization` are excluded because
#: neither has an operating business to draw a deck of -- a de-SPAC has no target
#: financials at all -- and `unclassified` is excluded because it means neither
#: classifier fired, which is an absence of evidence rather than a deal.
#:
#: `division_sale` **does** promote. The filer is a real operating company that
#: just sold a unit, which is worth a deck; what a division sale must never do is
#: contribute a *multiple*, and that exclusion lives in `deal_multiples` where the
#: arithmetic is.
DEAL_TYPES: Final[tuple[str, ...]] = ("operating", "division_sale")

#: Lookback for the Form 4 leg, in days, over `signals.occurred_at`. The measured
#: p90 of the gap between a cluster's first purchase and the day it became
#: visible; see the module docstring. A shorter window silently drops clusters.
FORM4_WINDOW_DAYS: Final[int] = 7

#: `cluster:<cik>:<role>:<first>`, the key `form4.cluster_key` writes into
#: `signals.accession`. Parsed rather than joined because the loader stores the
#: issuer's CIK nowhere else -- see :func:`cluster_cik`.
_CLUSTER_KEY: Final[re.Pattern[str]] = re.compile(
    r"^cluster:(\d+):([a-z_]+):(\d{4}-\d{2}-\d{2})$")


class PromoteError(RuntimeError):
    """The promoted set could not be built."""


def cluster_cik(key: str) -> str:
    """The issuer CIK out of a Form 4 cluster key, in `cik_key` form.

    **The CIK is in the key and nowhere else.** `form4.load` writes a payload
    carrying the symbol and the issuer name but not the issuer CIK, and sets
    `company_id` to null because a cluster can arrive before the entity resolves.
    So the only identifier on a stored cluster is the composite key, and this
    reads it out rather than matching `payload->>'issuer_name'` against
    `companies.name` -- which would be a name join, and the Form 5500 measurement
    put name matching at 44.2% precision against ground truth.

    Returns ``""`` for anything that is not a cluster key, so a malformed row is
    dropped by the caller's own resolution stage and counted there.
    """
    match = _CLUSTER_KEY.match(key or "")
    return cik_key(match.group(1)) if match else ""


@dataclass(frozen=True, slots=True)
class Promotion:
    """One filer promoted to Tier 2, and every reason it was."""

    cik: str
    company: str
    #: The symbol the signal came from -- the largest absolute move among this
    #: filer's symbols in the day's screen, or None for a filer promoted by a
    #: filing rather than by a price.
    #:
    #: **Not "the primary listing", because that is not knowable from what is
    #: stored.** Measured 2026-09-13: 1,452 of 8,005 filers carry more than one
    #: symbol, and on 527 of those the shortest is not a prefix of the rest --
    #: they are preferred series (AILIH, AILIM, AILIN...), ADR classes (AKZOF and
    #: AKZOY), share classes (BF-A, BF-B), or structured notes. `company_tickers`
    #: holds no exchange and no primary flag, so any rule picking one would be a
    #: guess: shortest-then-alphabetical resolves JPMorgan to **AMJB**.
    #:
    #: So this is the symbol that *earned the promotion*, which is a fact rather
    #: than a guess, and :attr:`tickers` keeps the rest.
    ticker: str | None
    #: Every symbol of this filer that the day's screen listed, sorted. Warrants
    #: and rights move differently from the common share -- Alliance
    #: Entertainment's AENTW warrant was +24.6% on 2026-09-11, in the sub-$1
    #: band, while its AENT common share was +16.5% in the $1-10 band -- a
    #: different percentage, a different band and a different tick count. A row
    #: taking whichever the database returned first reported a different fact on
    #: different runs of the same command.
    tickers: tuple[str, ...]
    #: In :data:`REASONS` order, so two filers promoted the same way sort together.
    reasons: tuple[str, ...]
    #: ``{reason: one line of why}``. Carried onto the deck and the log, because
    #: "promoted" with no reason is the thing nobody checks -- and a name promoted
    #: by a Form 4 cluster whose deck says "no Form 4 clusters on record" is a
    #: contradiction only this field makes visible.
    why: dict[str, str] = field(default_factory=dict)

    @property
    def multi(self) -> bool:
        """Promoted by more than one sentinel. The strongest signal there is."""
        return len(self.reasons) > 1


@dataclass(frozen=True, slots=True)
class Result:
    """The promoted set for one session, with what it cost to get there."""

    day: date
    rows: list[Promotion]
    funnel: funnel_mod.Funnel
    #: ``{reason: distinct filers}`` before the union, so a leg that contributed
    #: nothing is visible as a zero rather than as an absence.
    legs: dict[str, int]
    #: ``{reason: names that leg could not key on a CIK}``. An ETF has no CIK and
    #: never will; a stock that does not resolve is a gap in `company_tickers`.
    unresolved: dict[str, int]
    #: The Form 4 lookback actually used, for the line that reports it.
    form4_window: int = FORM4_WINDOW_DAYS

    @property
    def ciks(self) -> list[str]:
        return [r.cik for r in self.rows]


def _screen_tickers(screen_result: Any) -> tuple[set[str], int, int]:
    """Stock tickers from a volatility ScreenResult, and what was set aside.

    ETFs are dropped here rather than left to fail resolution, because they fail
    it for a structural reason rather than a fixable one: an ETF files no 10-K,
    has no CIK in `company_tickers`, and could never produce a fundamentals page.
    Counting them as "unresolved" would put 137 permanent absences in a column a
    reader is meant to read as a gap worth closing.
    """
    everything = {m.ticker for sl in screen_result.lists for m in sl.rows}
    stock = {
        m.ticker for sl in screen_result.lists
        if sl.security_type == "stock" for m in sl.rows
    }
    return stock, len(everything), len(everything) - len(stock)


def _resolve_tickers(con: Any, tickers: Iterable[str],
                     alias: str) -> dict[str, tuple[str, str]]:
    """``{ticker: (cik, company)}`` for current listings.

    **One row per ticker, chosen by an explicit order.** The schema comment on
    `company_tickers` says to expect a list back rather than a row -- share
    classes give one company several symbols and recycling gives one symbol
    several companies -- so this picks the most recently seen pair and breaks a
    tie on the CIK. Today no ticker in the table has two rows, which is exactly
    when a non-deterministic pick looks correct: `build_sponsors` produced three
    different review counts from byte-identical input for this reason.

    A recycled symbol still resolves to whoever holds it *now*, which is the
    right answer for a screen over today's session and the wrong one for
    history; the deck's identity page says so on the ticker row.
    """
    wanted = sorted({t for t in tickers if t})
    if not wanted:
        return {}
    values = ", ".join("'" + t.replace("'", "''") + "'" for t in wanted)
    sql = (
        f"select ticker, {cik_sql('cik')} as cik10, company from ("
        f"  select t.ticker as ticker, c.cik as cik, c.name as company "
        f"  from {alias}.company_tickers t "
        f"  join {alias}.companies c on c.id = t.company_id "
        f"  where t.ticker in ({values}) "
        f"  qualify row_number() over (partition by t.ticker "
        f"                             order by t.last_seen desc, c.cik) = 1"
        f")"
    )
    try:
        rows = con.execute(sql).fetchall()
    except Exception as exc:                       # a missing table, say
        raise PromoteError(
            f"could not resolve {len(wanted)} screen tickers to CIKs: {exc}"
        ) from exc
    return {t: (cik_key(cik), company or t) for t, cik, company in rows}


def promote(
    con: Any,
    *,
    day: date | None = None,
    screen_result: Any = None,
    alias: str = "pg",
    form4_window: int = FORM4_WINDOW_DAYS,
) -> Result:
    """The filers promoted to Tier 2 for one session.

    ``screen_result`` is a :class:`volatility.ScreenResult`; passing it in rather
    than screening here is the same contract `digest.build` has, and for the same
    reason -- the digest is the single reader for the day's moves, so the deck job
    and the morning email cannot disagree about which names moved.

    ``day`` defaults to that screen's own session rather than to today, so the
    promoted set is computed against the data that exists rather than against the
    calendar.
    """
    legs: dict[str, int] = {r: 0 for r in REASONS}
    unresolved: dict[str, int] = {r: 0 for r in REASONS}
    found: dict[str, dict[str, Any]] = {}
    raw = 0
    etfs = 0

    def add(cik: str, company: str, ticker: str | None, reason: str,
            why: str) -> None:
        """Merge one leg's hit into the filer's row, on explicit keys only.

        **Every tie here is broken by a rule rather than by arrival order**, and
        that is not theoretical tidiness. The first version kept whichever
        company name arrived first and whichever ticker arrived first; the reads
        below carry no `order by`, DuckDB's Postgres scanner is parallel, and
        three runs of the same command against the same session produced three
        different answers for five filers -- each time swapping a warrant for its
        common share and the move line with it.
        """
        slot = found.setdefault(
            cik, {"names": {}, "tickers": set(), "why": {}})
        # **By leg precedence, not by arrival and not by length.** Each leg reads
        # a different name column -- `companies.name` for the screen,
        # `deals.company` for a filing, nothing at all for a cluster -- so the
        # name is kept per leg and resolved by the order in `REASONS`.
        #
        # "The longest name wins" was the first attempt and is worse than the
        # coin flip it replaced: it is a rule, and the rule prefers a *longer*
        # string, which on this data is the wrong one. `companies.name` is the
        # entity table's own spelling and the shorter of the two on 3M ("3M CO"
        # against the filing's longer legal name), so length would have promoted
        # whichever source happened to be more verbose.
        if company:
            slot["names"][reason] = company
        if ticker:
            slot["tickers"].add(ticker)
        slot["why"][reason] = why

    # --- leg 1: the volatility screen -----------------------------------
    if screen_result is not None:
        day = day or screen_result.day
        stock, total, etfs = _screen_tickers(screen_result)
        raw += total
        resolved = _resolve_tickers(con, stock, alias)
        if stock and not resolved:
            # The cross-store assertion. An empty join is the one result that
            # looks like a correct answer about the data, and this one crosses
            # Parquet (the screen) into Postgres (the listings).
            raise PromoteError(
                f"0 of {len(stock)} stock tickers in the {day} screen resolved to "
                "a CIK. company_tickers holds 10,412 rows, so this is a join "
                "failure rather than a quiet day -- check the CIK representation "
                "on both sides before believing the promoted set."
            )
        unresolved[VOLATILITY] = len(stock) - len(resolved)
        for ticker in sorted(resolved):
            cik, company = resolved[ticker]
            if not cik:
                continue
            add(cik, company, ticker, VOLATILITY,
                "in the day's volatility screen")
        legs[VOLATILITY] = len({c for c, _ in resolved.values() if c})

    if day is None:
        raise PromoteError(
            "no day to promote for. Pass a volatility ScreenResult or a `day`; "
            "defaulting to the calendar would promote against data that may not "
            "have landed."
        )

    # --- leg 2: a qualifying deal filing --------------------------------
    types = ", ".join(f"'{t}'" for t in DEAL_TYPES)
    deal_rows = _query(con, alias, f"""
        select {cik_sql('cik')} as cik10, company, deal_type, filer_role, items
        from {alias}.deals
        where filed_date = date '{day.isoformat()}' and deal_type in ({types})
        -- Ordered because the merge above is order-sensitive and this read is
        -- not ordered by anything otherwise: DuckDB's Postgres scanner is
        -- parallel, and an unordered read is a coin flip taken once per run.
        order by cik, accession
    """, "deals")
    raw += len(deal_rows)
    seen_deals: set[str] = set()
    for cik10, company, deal_type, role, items in deal_rows:
        cik = cik_key(cik10)
        if not cik:
            unresolved[DEAL_FILING] += 1
            continue
        seen_deals.add(cik)
        add(cik, company or "", None, DEAL_FILING,
            f"8-K items {items} filed {day}, {deal_type}, filer is {role}")
    legs[DEAL_FILING] = len(seen_deals)

    # --- leg 3: a Form 4 purchase cluster -------------------------------
    # **`at time zone 'UTC'` is load-bearing, not belt-and-braces.** Reading
    # `signals` through the attached alias means DuckDB does the cast, and DuckDB
    # renders a timestamptz in the machine's zone -- so a cluster stored at
    # 2026-09-08 00:00:00+00 came back as 2026-09-07 on a US-Central box, a day
    # early on every row. `edgar_rss` casts the same column inside a
    # `postgres_query` string, where Postgres does the cast instead and the zone
    # is its own; the two paths are not interchangeable.
    since = day - timedelta(days=max(0, form4_window - 1))
    cluster_rows = _query(con, alias, f"""
        select accession, (occurred_at at time zone 'UTC')::date as on_day,
               payload->>'n_buyers' as n_buyers, payload->>'role' as role,
               payload->>'fund_like' as fund_like
        from {alias}.signals
        where kind = 'form4_cluster' and accession is not null
          and (occurred_at at time zone 'UTC')::date
              between date '{since.isoformat()}' and date '{day.isoformat()}'
        order by accession
    """, "signals")
    raw += len(cluster_rows)
    seen_clusters: set[str] = set()
    for accession, on_day, n_buyers, role, fund_like in cluster_rows:
        cik = cluster_cik(str(accession))
        if not cik:
            unresolved[FORM4_CLUSTER] += 1
            continue
        seen_clusters.add(cik)
        flag = " (fund-like buyers)" if str(fund_like).lower() == "true" else ""
        add(cik, "", None, FORM4_CLUSTER,
            f"{n_buyers} {role} buyers from {on_day}{flag}")
    legs[FORM4_CLUSTER] = len(seen_clusters)

    rows = []
    for cik, slot in found.items():
        symbols = tuple(sorted(slot["tickers"]))
        # The symbol that earned the promotion: largest absolute move, tie broken
        # on the symbol itself. Never "the first one we saw" and never a guess at
        # the primary listing -- see `Promotion.ticker` for why the data cannot
        # support one.
        primary = None
        if symbols and screen_result is not None:
            primary = max(symbols, key=lambda s: (_largest_move(screen_result, s), s))
        elif symbols:
            primary = symbols[0]
        why = dict(slot["why"])
        if primary and VOLATILITY in why:
            why[VOLATILITY] = f"{primary} {_move_for(screen_result, primary)}"
            if len(symbols) > 1:
                # Said out loud rather than resolved silently: a filer with a
                # warrant in the same screen list has two different facts about
                # it, and the deck only draws one of them.
                why[VOLATILITY] += (
                    f" -- {len(symbols)} of this filer's symbols were in the "
                    f"screen ({', '.join(symbols)}), and a warrant does not "
                    "move with its common share")
        names = slot["names"]
        rows.append(Promotion(
            cik=cik,
            company=next((names[r] for r in REASONS if names.get(r)), cik),
            ticker=primary,
            tickers=symbols,
            reasons=tuple(r for r in REASONS if r in slot["why"]),
            why=why,
        ))
    # Strongest signal first: a filer three sentinels agree on, then by name. An
    # explicit key rather than insertion order, so two runs promote in one order.
    rows.sort(key=lambda p: (-len(p.reasons), p.company, p.cik))

    keyed = sum(legs.values())
    fn = funnel_mod.build(
        SCREEN,
        ("sentinel hits", raw,
         "every screen row, deal filing and cluster the three legs saw"),
        ("not an ETF", raw - etfs,
         "an ETF files no 10-K and has no CIK; it could never carry a deck"),
        ("keyed on a CIK", raw - etfs - sum(unresolved.values()),
         "resolved through company_tickers, the deals CIK column, or the "
         "cluster key -- never through a name"),
        ("distinct filers", len(rows),
         f"one row per CIK; {sum(1 for p in rows if p.multi)} promoted by more "
         "than one sentinel"),
    )
    log.info("promoted %d filers for %s (%s)", len(rows), day,
             ", ".join(f"{k}={v}" for k, v in legs.items()))
    return Result(day=day, rows=rows, funnel=fn, legs=legs,
                  unresolved=unresolved, form4_window=form4_window)


def _largest_move(screen_result: Any, ticker: str) -> float:
    """The biggest absolute one-day move this symbol made, or 0.

    The ranking key for "which of a filer's symbols earned the promotion". A
    warrant and its common share are the same CIK and not the same security, and
    on 2026-09-11 the warrant was the one that moved: AENTW +24.6% against AENT
    +16.5%.

    **So this is "which symbol put the name in front of you", and it is
    deliberately not "the common share".** The larger move is a fact about the
    day; the common share is not identifiable from what is stored -- see
    :attr:`Promotion.ticker`. Where the two differ the answer is to say so rather
    than to pick, which is what :attr:`Promotion.tickers` and the reason line are
    for. A deck of a filer whose promoted symbol is a warrant draws the warrant's
    chart under the company's fundamentals, and the reason line beside it says
    which symbols were in the screen.
    """
    return max((abs(float(m.pct_move))
                for sl in screen_result.lists for m in sl.rows
                if m.ticker == ticker), default=0.0)


def _move_for(screen_result: Any, ticker: str) -> str:
    """The largest move this ticker made on the session, as a sentence.

    `max` on an explicit key rather than the first list it appears in: a ticker
    can be in four lists and which one is read must not depend on list order.
    """
    hits = [m for sl in screen_result.lists for m in sl.rows if m.ticker == ticker]
    if not hits:
        return "in the day's volatility screen"
    worst = max(hits, key=lambda m: (abs(float(m.pct_move)), m.band))
    # `pct_move` is already a percentage, not a fraction -- the digest renders it
    # as `{m.pct_move:>7.2f}%`. The first version of this line multiplied by 100
    # and printed "+854.0%" for an 8.5% day, which is the kind of wrong that only
    # a human reading the output catches: every figure was plausible in shape and
    # absurd in size.
    return (f"{float(worst.pct_move):+.1f}% on {screen_result.day} "
            f"in the {worst.band} band, {worst.tick_move:+.0f} ticks")


def _query(con: Any, alias: str, sql: str, what: str) -> list[tuple]:
    """One read, with the table named in the refusal rather than a traceback."""
    try:
        return con.execute(sql).fetchall()
    except Exception as exc:
        raise PromoteError(f"could not read {what}: {exc}") from exc


# --- the gate ----------------------------------------------------------
#
# Separate from `promote` because it answers a different question and has a
# different input. Promotion asks what the sentinels noticed; the gate asks what
# there is to draw, which depends on the XBRL and DCF state of the machine rather
# than on the session.


#: The gate's one stage name. The *only* hard condition, and one rather than
#: four: a valuation already implies fundamentals (the DCF cannot run without
#: free cash flow), while comps, insiders, deals and prices each have a page that
#: says "none on record" rather than a page that goes blank.
REQUIRED_STAGE: Final[str] = "has a valuation"


@dataclass(frozen=True, slots=True)
class Gated:
    """The promoted set split into what will render and what will not."""

    day: date
    kept: list[Promotion]
    #: ``[(promotion, why not)]``. Reported as a count with the reasons under it,
    #: which is the whole point: a name that cannot produce a deck is a thing to
    #: know about, not a file to write.
    skipped: list[tuple[Promotion, str]]
    funnel: funnel_mod.Funnel
    #: How many of the kept set carry each optional input, so a run can say "27
    #: decks, 12 of them with a peer set" rather than implying all ten pages are
    #: populated on all of them.
    optional: dict[str, int] = field(default_factory=dict)


def gate(
    result: Result,
    *,
    valued: set[str],
    with_comps: set[str] | None = None,
    with_prices: set[str] | None = None,
) -> Gated:
    """Which promoted filers have enough behind them to be worth ten pages.

    ``valued`` is the CIKs the DCF produced an enterprise value for, in
    :func:`cik_key` form. **Both sides are keyed, and that is not decoration:**
    `_dcf_rows` returns CIKs unpadded and everything in Postgres is padded to ten,
    and the first measurement of this gate reported `valuation=0` for all 185
    promoted names -- a perfectly believable answer about a mover list full of
    penny stocks, and wrong.

    The funnel continues the promotion funnel rather than starting a new one, so
    one table runs from every sentinel hit to every file written.
    """
    keyed_valued = {cik_key(c) for c in valued}
    if valued and not keyed_valued:
        raise PromoteError("the valued set keyed to nothing; check its CIKs")

    kept: list[Promotion] = []
    skipped: list[tuple[Promotion, str]] = []
    for row in result.rows:
        if row.cik in keyed_valued:
            kept.append(row)
        else:
            skipped.append((row, "no DCF valuation, so nine of the ten pages "
                                 "would carry no number"))

    optional: dict[str, int] = {}
    for name, pool in (("peer set", with_comps), ("price history", with_prices)):
        if pool is None:
            continue
        keyed = {cik_key(c) for c in pool}
        optional[name] = sum(1 for r in kept if r.cik in keyed)

    stages = [(s.name, s.remaining, s.why) for s in result.funnel.stages]
    stages.append((
        REQUIRED_STAGE, len(kept),
        "the one input a deck cannot substitute for; the rest of the pages say "
        "'none on record' instead of going blank",
    ))
    return Gated(day=result.day, kept=kept, skipped=skipped,
                 funnel=funnel_mod.build(SCREEN, *stages), optional=optional)


def render(result: Result, gated: Gated | None = None) -> list[str]:
    """The promoted set as text, for the CLI and the scheduled task's log.

    The log is the only place an unattended run is visible, so it carries the
    funnel and the per-leg counts rather than just the number of files -- a job
    that emits four decks because the screen broke and a job that emits four
    because it was a quiet Tuesday are indistinguishable from the count alone.
    """
    out = [f"promoted set for {result.day}"]
    for reason in REASONS:
        line = f"  {reason:<14} {result.legs.get(reason, 0):>4} filers"
        if reason == FORM4_CLUSTER:
            line += (f"   (occurred_at within {result.form4_window}d; that is the "
                     "measured p90 of the filing lag, so ~10% of clusters land "
                     "outside it)")
        if result.unresolved.get(reason):
            line += f"   {result.unresolved[reason]} unkeyed"
        out.append(line)
    out.append(f"  {'union':<14} {len(result.rows):>4} filers, "
               f"{sum(1 for p in result.rows if p.multi)} on more than one leg")
    out.append("")
    out.extend((gated.funnel if gated else result.funnel).lines())
    if gated is not None:
        out.append("")
        out.append(f"  {len(gated.kept)} of {len(result.rows)} promoted filers "
                   f"can produce a deck; {len(gated.skipped)} cannot and are "
                   "reported rather than rendered")
        for name, n in sorted(gated.optional.items()):
            out.append(f"    of those, {n} carry a {name}")
    return out
