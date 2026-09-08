"""The daily email: macro line, then the screens.

Reads only. It renders what the loaders already stored and sends it; it
computes no new state and writes nothing back, so ``--dry-run`` is a complete
exercise of everything except the HTTP POST.

Structure is deliberate. The macro line comes first because it is the frame:
a 4% move in a $30 stock reads differently when the 10-year has just moved 15
basis points and high yield is widening. Then the screens, in the order the
funnel narrows — liquid names first, since those are the only ones that can
be acted on, with the ungated lists below for the ones that are interesting
but untradeable.

``--dry-run`` never constructs a client and never reads a credential, so it
works on a machine with no Resend key at all. That is the point: the thing
most likely to be wrong is the rendering, and checking it should not require
being able to send mail.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Final, Iterator

import duckdb

from marketradar import manifest, storage
from marketradar.clock import market_today
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

#: Rows per list in the email. The screens hold 20; twenty-four lists of
#: twenty is 480 rows, which is a spreadsheet, not something read over
#: coffee. The full depth stays available in `mr screens`.
EMAIL_TOP_N: Final[int] = 10

#: Lookbacks on the macro line, in calendar days.
MACRO_LOOKBACKS: Final[tuple[tuple[int, str], ...]] = ((30, "30d"), (365, "1y"))

REQUEST_TIMEOUT: Final[float] = 30.0


class DigestError(RuntimeError):
    """The digest could not be built or sent."""


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
    generated_at: datetime
    macro: list[MacroLine]
    screen: volatility.ScreenResult
    top_n: int

    @property
    def subject(self) -> str:
        return f"Market Radar - {self.day.isoformat()}"


def build(
    con: duckdb.DuckDBPyConnection | None = None,
    *,
    as_of: date | None = None,
    top_n: int = EMAIL_TOP_N,
    screen_top_n: int = volatility.TOP_N,
) -> Digest:
    """Assemble the digest from stored data. No network beyond the warehouse."""
    con = con or storage.connect()

    result = volatility.screen(con, as_of=as_of, top_n=screen_top_n)

    macro: list[MacroLine] = []
    latest = fred.latest(con)
    for series in fred.SERIES:
        obs = latest.get(series.series_id)
        if obs is None or obs.value is None:
            log.warning("no observation for %s; omitting from the macro line",
                        series.series_id)
            continue
        macro.append(
            MacroLine(
                label=series.label,
                value=obs.value,
                units=series.units,
                as_of=obs.obs_date,
                changes={
                    name: fred.change_since(con, series.series_id, days)
                    for days, name in MACRO_LOOKBACKS
                },
            )
        )

    return Digest(
        day=result.day,
        generated_at=datetime.now(timezone.utc),
        macro=macro,
        screen=result,
        top_n=top_n,
    )


def _signed(value: Decimal | None, units: str) -> str:
    """Render a *change*, which is not the same unit as the level.

    A 10-year yield going 4.63 -> 4.77 is "+14bp", not "+0.14%". Writing the
    change with a percent sign invites reading it as a relative move, and for
    a series already quoted in percent that is off by two orders of magnitude.
    Basis points are the convention for both yields and option-adjusted
    spreads, so the level keeps its % and the change gets bp.
    """
    if value is None:
        return "n/a"
    if units == "%":
        return f"{value * 100:+.0f}bp"
    return f"{value:+.2f}{units}"


def _ordered_lists(digest: Digest) -> list[volatility.ScreenList]:
    """Liquid first, then ungated. Actionable before merely interesting."""
    lists = [sl for sl in digest.screen.lists if sl.rows]
    liquid = [sl for sl in lists if sl.liquidity == "liquid"]
    rest = [sl for sl in lists if sl.liquidity != "liquid"]
    return liquid + rest


def render_text(digest: Digest) -> str:
    return "\n".join(_text_lines(digest))


def _text_lines(digest: Digest) -> Iterator[str]:
    yield f"Market Radar - {digest.day.isoformat()}"
    yield f"generated {digest.generated_at.strftime('%Y-%m-%d %H:%M')} UTC"
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
    yield "SCREENS"
    yield (
        f"  {s.moves_screened:,} moves screened for {s.day.isoformat()}"
        f"; {s.floor_excluded:,} below the ${s.sanity_floor} sanity floor"
    )
    yield f"  showing top {digest.top_n} per list; full depth in `mr screens`"

    for sl in _ordered_lists(digest):
        rows = sl.rows[: digest.top_n]
        yield ""
        yield f"--- {sl.title} ---"
        yield (
            f"    {'ticker':<10} {'pct':>9} {'ticks':>10}  "
            f"{'prev':>11} {'close':>11}  {'adv':>8}  flags"
        )
        for m in rows:
            flags = []
            if m.split_factor != 1:
                flags.append(f"split x{m.split_factor.normalize()}")
            if m.is_ex_div:
                flags.append(f"ex-div {m.div_cash.normalize()}")
            yield (
                f"    {m.ticker:<10} {m.pct_move:>8.2f}% {m.tick_move:>10.1f}  "
                f"{m.adj_prev_close:>11} {m.close:>11}  "
                f"{volatility._fmt_money(m.avg_dollar_volume):>8}  "
                f"{', '.join(flags)}"
            )
        if len(sl.rows) > digest.top_n:
            yield f"    ... {len(sl.rows) - digest.top_n} more"

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
