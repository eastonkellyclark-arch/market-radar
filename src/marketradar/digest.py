"""The daily email: health, macro, then the screens.

Reads only. It renders what the loaders already stored and sends it; it
computes no new state and writes nothing back, so ``--dry-run`` is a complete
exercise of everything except the HTTP POST.

The order is the order the questions get asked.

**Health first**, because a degraded run must not look like a healthy one. A
sweep that covered 60% of the universe still produces twenty plausible lists,
and nothing else on the page would tell you.

**Macro second**, because it is the frame: a 4% move in a $30 stock reads
differently when the ten-year has just moved 15bp and high yield is widening.

**Then the screens**, liquid only by default. The ungated lists are behind
``--all``: illiquid names are often the interesting ones, but they are not the
thing being scanned over coffee.

Names are joined from ``companies``, and rows are marked ``NEW`` when the
ticker was not in the same list on the previous session — that is the question
being asked when a list is scanned, and answering it from memory is what the
digest is supposed to replace.

``--dry-run`` never constructs a client and never reads a credential, so it
works on a machine with no Resend key at all. The thing most likely to be
wrong is the rendering, and checking it should not require being able to send
mail.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Final, Iterable, Iterator

import duckdb

from marketradar import manifest, storage
from marketradar.screens import volatility
from marketradar.sources import fred

log = logging.getLogger(__name__)

ENV_API_KEY: Final[str] = "MR_RESEND_API_KEY"
ENV_FROM: Final[str] = "MR_DIGEST_FROM"
ENV_TO: Final[str] = "MR_DIGEST_TO"

#: Resend's shared sender. Works with no domain verification, which is what
#: makes the first send possible before DNS is set up. Deliverability is poor
#: and it cannot be used for anything but testing, so this is a placeholder
#: for a verified domain, not a destination.
DEFAULT_FROM: Final[str] = "onboarding@resend.dev"

#: Rows per list in the email. The screens hold 20; the email shows the top of
#: each and points at `mr screens` for the rest.
EMAIL_TOP_N: Final[int] = 10

#: Lookbacks on the macro line, in calendar days.
MACRO_LOOKBACKS: Final[tuple[tuple[int, str], ...]] = ((30, "30d"), (365, "1y"))

#: A sweep covering less than this share of the best day in the partition is
#: reported as degraded. Compared within the partition rather than against the
#: live universe so the check needs no network and cannot itself fail.
SWEEP_COVERAGE_FLOOR: Final[float] = 0.95

#: Trading days the price data may lag before it is called stale. Wide enough
#: for a Friday close read after a Monday holiday.
PRICE_STALENESS_DAYS: Final[int] = 4

REQUEST_TIMEOUT: Final[float] = 30.0


class DigestError(RuntimeError):
    """The digest could not be built or sent."""


# --- health -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HealthItem:
    name: str
    detail: str
    ok: bool = True
    note: str = ""


@dataclass(frozen=True, slots=True)
class Health:
    items: list[HealthItem] = field(default_factory=list)

    @property
    def problems(self) -> list[HealthItem]:
        return [i for i in self.items if not i.ok]

    @property
    def degraded(self) -> bool:
        return bool(self.problems)

    @property
    def status(self) -> str:
        if not self.degraded:
            return "OK"
        n = len(self.problems)
        return f"DEGRADED - {n} problem{'s' if n != 1 else ''}"


def _latest_macro(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Latest macro prints, or {} if the table is unreachable.

    Non-fatal on purpose. The loaders already assert on bad macro data; by the
    time the digest runs, a missing table means the screens should still be
    readable with the macro line reported as absent. A digest that refuses to
    render is less useful than one that opens with what is wrong.
    """
    try:
        return fred.latest(con)
    except Exception as exc:
        log.warning("macro lookup failed: %s", exc)
        return {}


def check_health(
    con: duckdb.DuckDBPyConnection,
    *,
    day: date,
    prices: duckdb.DuckDBPyRelation,
    prior_day: date | None,
) -> Health:
    """What would otherwise be invisible: partial sweeps and stalled feeds.

    Deliberately not an assertion. ``assert_fresh`` already fails the *loaders*
    on bad data; by the time the digest runs, whatever is stored is what there
    is, and the job here is to say so on the page rather than to refuse to
    render. A digest that will not print is less useful than one that opens
    with what is wrong.
    """
    items: list[HealthItem] = []
    con.register("health_px", prices)

    # Coverage: how many symbols the screened session carries against the best
    # day in the partition. A truncated sweep still produces plausible lists.
    rows = con.execute(
        "select date, count(distinct ticker) from health_px group by 1 order by 1"
    ).fetchall()
    by_date = {r[0]: int(r[1]) for r in rows}
    swept = by_date.get(day, 0)
    expected = max(by_date.values()) if by_date else 0
    if expected:
        share = swept / expected
        ok = share >= SWEEP_COVERAGE_FLOOR
        items.append(
            HealthItem(
                name="prices_eod_raw",
                detail=f"{swept:,} / {expected:,} symbols ({share:.0%})",
                ok=ok,
                note="" if ok else "sweep looks truncated",
            )
        )
    else:
        items.append(
            HealthItem("prices_eod_raw", "no price data", ok=False,
                       note="nothing to screen")
        )

    newest = max(by_date) if by_date else None
    if newest is not None:
        from marketradar.clock import market_today

        age = (market_today() - newest).days
        items.append(
            HealthItem(
                name="prices max_date",
                detail=f"{newest.isoformat()} ({age}d old)",
                ok=age <= PRICE_STALENESS_DAYS,
                note="" if age <= PRICE_STALENESS_DAYS else "feed may have stopped",
            )
        )

    # Macro, per series. A combined check passes whenever any one is current.
    latest = _latest_macro(con)
    from marketradar.clock import market_today

    today = market_today()
    for series in fred.SERIES:
        obs = latest.get(series.series_id)
        if obs is None:
            items.append(
                HealthItem(series.series_id, "not loaded", ok=False,
                           note="run `mr fred`")
            )
            continue
        age = (today - obs.obs_date).days
        ok = age <= fred.MAX_STALENESS_DAYS
        items.append(
            HealthItem(
                name=series.series_id,
                detail=f"{obs.obs_date.isoformat()} ({age}d old)",
                ok=ok,
                note="" if ok else "stalled",
            )
        )

    # Entity coverage. Not a failure — about half the universe has no CIK by
    # construction — but the number should not move without a reason.
    try:
        counts = con.execute(
            "SELECT * FROM postgres_query('pg', ?)",
            # Aliased: two bare count(*) columns come back both named "count"
            # and DuckDB refuses the duplicate.
            ["select (select count(*) from companies) as n_companies, "
             "(select count(*) from company_tickers) as n_tickers"],
        ).fetchone()
        items.append(
            HealthItem(
                "companies",
                f"{int(counts[0]):,} CIKs, {int(counts[1]):,} ticker rows",
            )
        )
    except Exception as exc:  # pragma: no cover - needs a live database
        items.append(HealthItem("companies", f"unavailable: {exc}"[:80], ok=False))

    if prior_day is None:
        items.append(
            HealthItem(
                "day-over-day",
                "no prior session in the data",
                ok=False,
                note="NEW markers unavailable; widen the sweep window",
            )
        )
    else:
        items.append(
            HealthItem("day-over-day", f"comparing against {prior_day.isoformat()}")
        )

    return Health(items=items)


# --- names --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Name:
    name: str
    ambiguous: bool


def names_for(
    con: duckdb.DuckDBPyConnection, tickers: Iterable[str]
) -> dict[str, Name]:
    """Company names for display, in one query.

    A ticker is not a join key — share classes and recycling both give a
    ticker more than one company — so this is a *display* lookup and says so.
    Where a ticker resolves to several companies the most recently seen one is
    shown and marked ambiguous, rather than a silent pick. About half the
    universe has no CIK at all, so a missing name is normal, not an error.
    """
    wanted = sorted({t for t in tickers if t})
    if not wanted:
        return {}
    quoted = ", ".join("'" + t.replace("'", "''") + "'" for t in wanted)
    try:
        rows = con.execute(
            "SELECT * FROM postgres_query('pg', ?)",
            [
                "select ct.ticker, c.name, c.id, ct.last_seen "
                "from company_tickers ct join companies c on c.id = ct.company_id "
                f"where ct.ticker in ({quoted})"
            ],
        ).fetchall()
    except Exception as exc:  # pragma: no cover - needs a live database
        log.warning("name lookup failed, rendering without names: %s", exc)
        return {}

    grouped: dict[str, list[tuple]] = {}
    for ticker, name, company_id, last_seen in rows:
        grouped.setdefault(ticker, []).append((last_seen, company_id, name))

    out: dict[str, Name] = {}
    for ticker, entries in grouped.items():
        entries.sort(key=lambda e: (e[0] is not None, e[0], e[1]), reverse=True)
        out[ticker] = Name(
            name=entries[0][2],
            ambiguous=len({e[1] for e in entries}) > 1,
        )
    return out


# --- assembly -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MacroLine:
    label: str
    value: Decimal
    units: str
    as_of: date
    changes: dict[str, Decimal | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Digest:
    day: date
    prior_day: date | None
    generated_at: datetime
    health: Health
    macro: list[MacroLine]
    screen: volatility.ScreenResult
    names: dict[str, Name]
    new_tickers: dict[tuple[str, str, str, str], set[str]]
    top_n: int
    include_ungated: bool

    @property
    def subject(self) -> str:
        flag = " [DEGRADED]" if self.health.degraded else ""
        return f"Market Radar - {self.day.isoformat()}{flag}"


def _change_since(
    con: duckdb.DuckDBPyConnection, series_id: str, days: int
) -> Decimal | None:
    """A lookback that cannot be computed renders as n/a, not as an error."""
    try:
        return fred.change_since(con, series_id, days)
    except Exception as exc:
        log.warning("change_since(%s, %d) failed: %s", series_id, days, exc)
        return None


def _list_key(sl: volatility.ScreenList) -> tuple[str, str, str, str]:
    return (sl.security_type, sl.band, sl.direction, sl.liquidity)


def build(
    con: duckdb.DuckDBPyConnection | None = None,
    *,
    as_of: date | None = None,
    top_n: int = EMAIL_TOP_N,
    screen_top_n: int = volatility.TOP_N,
    include_ungated: bool = False,
    prices: duckdb.DuckDBPyRelation | None = None,
    actions: duckdb.DuckDBPyRelation | None = None,
) -> Digest:
    """Assemble the digest from stored data. No network beyond the warehouse.

    ``prices`` and ``actions`` are injectable for the same reason they are on
    :func:`volatility.screen` — the day-over-day comparison needs two sessions
    to exercise, and requiring a full warehouse to check that is how the
    feature would go untested. Left unset they resolve through the manifest.
    """
    con = con or storage.connect()

    prices = volatility.read_prices(con, as_of) if prices is None else prices
    actions = volatility.read_actions(con) if actions is None else actions

    result = volatility.screen(
        con, as_of=as_of, prices=prices, actions=actions, top_n=screen_top_n
    )

    # The previous session with a computable move, for the NEW markers.
    all_moves = volatility.moves(con, prices=prices, actions=actions)
    con.register("digest_moves", all_moves)
    prior_row = con.execute(
        "select max(date) from digest_moves where passes_floor and date < ?",
        [result.day],
    ).fetchone()
    prior_day = prior_row[0] if prior_row else None

    new_tickers: dict[tuple[str, str, str, str], set[str]] = {}
    if prior_day is not None:
        previous = volatility.screen(
            con, as_of=prior_day, prices=prices, actions=actions, top_n=screen_top_n
        )
        before = {
            _list_key(sl): {m.ticker for m in sl.rows} for sl in previous.lists
        }
        for sl in result.lists:
            key = _list_key(sl)
            new_tickers[key] = {
                m.ticker for m in sl.rows if m.ticker not in before.get(key, set())
            }

    health = check_health(con, day=result.day, prices=prices, prior_day=prior_day)

    macro: list[MacroLine] = []
    latest = _latest_macro(con)
    for series in fred.SERIES:
        obs = latest.get(series.series_id)
        if obs is None or obs.value is None:
            continue
        macro.append(
            MacroLine(
                label=series.label,
                value=obs.value,
                units=series.units,
                as_of=obs.obs_date,
                changes={
                    name: _change_since(con, series.series_id, days)
                    for days, name in MACRO_LOOKBACKS
                },
            )
        )

    shown = [
        sl for sl in result.lists
        if sl.rows and (include_ungated or sl.liquidity == "liquid")
    ]
    names = names_for(
        con, [m.ticker for sl in shown for m in sl.rows[:top_n]]
    )

    return Digest(
        day=result.day,
        prior_day=prior_day,
        generated_at=datetime.now(timezone.utc),
        health=health,
        macro=macro,
        screen=result,
        names=names,
        new_tickers=new_tickers,
        top_n=top_n,
        include_ungated=include_ungated,
    )


# --- rendering ----------------------------------------------------------


def _signed(value: Decimal | None, units: str) -> str:
    """Render a *change*, which is not the same unit as the level.

    A 10-year yield going 4.63 -> 4.77 is "+14bp", not "+0.14%". Writing the
    change with a percent sign invites reading it as a relative move, and for
    a series already quoted in percent that is off by two orders of magnitude.
    """
    if value is None:
        return "n/a"
    if units == "%":
        return f"{value * 100:+.0f}bp"
    return f"{value:+.2f}{units}"


def _visible_lists(digest: Digest) -> list[volatility.ScreenList]:
    """Liquid first, then ungated if asked for. Actionable before interesting."""
    lists = [sl for sl in digest.screen.lists if sl.rows]
    liquid = [sl for sl in lists if sl.liquidity == "liquid"]
    if not digest.include_ungated:
        return liquid
    return liquid + [sl for sl in lists if sl.liquidity != "liquid"]


def render_text(digest: Digest) -> str:
    return "\n".join(_text_lines(digest))


def _text_lines(digest: Digest) -> Iterator[str]:
    yield f"Market Radar - {digest.day.isoformat()}"
    yield f"generated {digest.generated_at.strftime('%Y-%m-%d %H:%M')} UTC"
    yield ""

    yield f"HEALTH  {digest.health.status}"
    for item in digest.health.items:
        mark = " " if item.ok else "!"
        note = f"   <- {item.note}" if item.note else ""
        yield f"  {mark} {item.name:<18} {item.detail}{note}"
    yield ""

    yield "MACRO"
    if not digest.macro:
        yield "  (no macro data loaded -- run `mr fred`)"
    for line in digest.macro:
        changes = "  ".join(
            f"{name} {_signed(line.changes.get(name), line.units):>7}"
            for _, name in MACRO_LOOKBACKS
        )
        yield (
            f"  {line.label:<14} {line.value:>6.2f}{line.units}"
            f"   {changes}   as of {line.as_of.isoformat()}"
        )
    yield ""

    s = digest.screen
    lists = _visible_lists(digest)
    yield "SCREENS"
    yield (
        f"  {s.moves_screened:,} moves screened for {s.day.isoformat()}"
        f"; {s.floor_excluded:,} below the ${s.sanity_floor} sanity floor"
    )
    if digest.prior_day:
        yield f"  NEW = not in this list on {digest.prior_day.isoformat()}"
    else:
        yield "  NEW markers unavailable: no prior session in the data"
    scope = "liquid and ungated" if digest.include_ungated else ">$5M ADV only"
    yield (
        f"  {len(lists)} lists, {scope}, top {digest.top_n} each"
        f"{'' if digest.include_ungated else ' -- `mr digest --all` for the rest'}"
    )

    for sl in lists:
        rows = sl.rows[: digest.top_n]
        fresh = digest.new_tickers.get(_list_key(sl), set())
        yield ""
        yield f"--- {sl.title} ---"
        yield (
            f"    {'':3} {'ticker':<8} {'company':<28} {'pct':>8} {'ticks':>9}  "
            f"{'close':>10} {'adv':>7}  flags"
        )
        for m in rows:
            entry = digest.names.get(m.ticker)
            company = entry.name[:26] + (" ?" if entry.ambiguous else "") if entry else ""
            flags = []
            if m.split_factor != 1:
                flags.append(f"split x{m.split_factor.normalize()}")
            if m.is_ex_div:
                flags.append(f"ex-div {m.div_cash.normalize()}")
            yield (
                f"    {'NEW' if m.ticker in fresh else '':3} {m.ticker:<8} "
                f"{company:<28} {m.pct_move:>7.2f}% {m.tick_move:>9.1f}  "
                f"{m.close:>10.4f} "
                f"{volatility._fmt_money(m.avg_dollar_volume):>7}  "
                f"{', '.join(flags)}"
            )
        if len(sl.rows) > digest.top_n:
            yield f"    {'':3} ... {len(sl.rows) - digest.top_n} more"

    yield ""
    yield "-- "
    yield "Market Radar. Information only; this system sends no outreach."


def render_html(digest: Digest) -> str:
    """A monospace block. The text *is* the layout, so wrap rather than rebuild.

    Rebuilding this as tables would mean two renderings to keep in step and
    two chances for them to disagree about what the screen said.
    """
    from html import escape

    body = escape(render_text(digest))
    return (
        "<html><body style=\"margin:0;padding:16px;"
        "background:#ffffff;color:#111111;\">"
        "<pre style=\"font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,"
        "Consolas,monospace;white-space:pre;overflow-x:auto;\">"
        f"{body}"
        "</pre></body></html>"
    )


# --- sending ------------------------------------------------------------


def recipients() -> list[str]:
    raw = os.environ.get(ENV_TO, "").strip()
    if not raw:
        raise DigestError(
            f"{ENV_TO} is not set; there is nobody to send the digest to."
        )
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


def send(digest: Digest, *, dry_run: bool = False) -> dict[str, Any]:
    """Send via Resend, or render only.

    ``dry_run`` returns before any credential is read and before any client is
    constructed, so it runs on a machine with no Resend key.
    """
    if dry_run:
        return {"sent": False, "reason": "dry-run", "subject": digest.subject}

    key = os.environ.get(ENV_API_KEY, "").strip()
    if not key:
        raise DigestError(f"{ENV_API_KEY} is not set; cannot send.")
    sender = os.environ.get(ENV_FROM, "").strip() or DEFAULT_FROM
    to = recipients()

    import httpx

    url = manifest.get("resend_api", "send").location
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "from": sender,
                "to": to,
                "subject": digest.subject,
                "text": render_text(digest),
                "html": render_html(digest),
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise DigestError(
            f"Resend returned HTTP {exc.response.status_code}: "
            f"{exc.response.text[:300]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise DigestError(f"Could not reach Resend: {exc}") from exc

    return {"sent": True, "to": to, "from": sender, "response": resp.json()}
