"""Entity identity: normalisation, and the rules about what may be joined on.

Two jobs.

**Normalisation.** Turn a company name into a comparable key. Built for the
Form 5500 problem coming in Weekend 4, where sponsor names are DBAs, legal
entity names, and subsidiary rollups that all differ from how a company is
known. Getting this right against SEC names first — which are clean — means
the messy side has a stable target to match against.

**Join rules, enforced by API shape rather than by comment.** CIK is the join
key. A ticker is not, and never will be: share classes give one company
several tickers at once (BRK.A/BRK.B, GOOG/GOOGL) and recycling gives one
ticker several companies across time. So :func:`company_ids_for_ticker`
returns a *list*. There is no function here that maps a ticker to a single
company, because writing one would be writing the bug.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Final

#: Stripped from the end of a name, repeatedly, longest first. Order matters:
#: "Holdings Inc" must lose "inc" then "holdings".
ENTITY_SUFFIXES: Final[tuple[str, ...]] = (
    "incorporated",
    "corporation",
    "companies",
    "company",
    "holdings",
    "holding",
    "limited",
    "partners",
    "group",
    "trust",
    "plc",
    "llc",
    "llp",
    "ltd",
    "inc",
    "corp",
    "co",
    "lp",
    "sa",
    "nv",
    "ag",
    "ab",
    "as",
    "oy",
)

#: Common share-class and security-type tails on SEC titles.
CLASS_SUFFIXES: Final[tuple[str, ...]] = (
    "common stock",
    "common shares",
    "ordinary shares",
    "class a",
    "class b",
    "class c",
    "series a",
    "series b",
)

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")
_LEADING_THE = re.compile(r"^the\s+")


def normalize(name: str) -> str:
    """Reduce a company name to a fuzzy-matchable key.

    Lowercases, strips accents and punctuation, drops a leading "the", and
    removes entity and share-class suffixes repeatedly until none remain.

        >>> normalize("The Coca-Cola Company")
        'coca cola'
        >>> normalize("Berkshire Hathaway Inc.")
        'berkshire hathaway'
        >>> normalize("Acme Holdings, LLC")
        'acme'

    Never returns empty for a non-empty input: if stripping would consume the
    whole name, the last meaningful form is kept. "Holdings Inc" is a real
    sponsor name in Form 5500 data, and reducing it to "" would make it match
    everything.
    """
    if not name:
        return ""

    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = text.replace("&", " and ")
    text = _PUNCT.sub(" ", text)
    text = _SPACE.sub(" ", text).strip()
    text = _LEADING_THE.sub("", text)

    for suffix in CLASS_SUFFIXES:
        if text.endswith(" " + suffix):
            candidate = text[: -len(suffix)].strip()
            if candidate:
                text = candidate

    changed = True
    while changed:
        changed = False
        for suffix in ENTITY_SUFFIXES:
            if text.endswith(" " + suffix):
                candidate = text[: -(len(suffix) + 1)].strip()
                if candidate:  # never strip down to nothing
                    text = candidate
                    changed = True
                    break

    return _SPACE.sub(" ", text).strip()


def normalize_cik(value: Any) -> str | None:
    """CIKs are 10-digit zero-padded strings, everywhere, always.

    SEC publishes them as integers in some files and padded strings in others.
    Storing both forms means half your joins silently miss.

        >>> normalize_cik(320193)
        '0000320193'
        >>> normalize_cik("0000320193")
        '0000320193'
    """
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return None
    return digits.zfill(10)


def normalize_ticker(value: Any) -> str | None:
    """Uppercase, trimmed. Not a join key — see the module docstring."""
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


@dataclass(frozen=True, slots=True)
class TickerMatch:
    """One ticker's mapping to one company, at one point in time."""

    company_id: int
    cik: str | None
    ticker: str
    name: str
    source: str


def company_id_for_cik(con: Any, cik: str) -> int | None:
    """The join key. CIK identifies a filer uniquely and permanently."""
    key = normalize_cik(cik)
    if key is None:
        return None
    rows = con.execute(
        "SELECT * FROM postgres_query('pg', ?)",
        [f"select id from companies where cik = '{key}' limit 1"],
    ).fetchall()
    return int(rows[0][0]) if rows else None


def company_ids_for_ticker(con: Any, ticker: str) -> list[TickerMatch]:
    """Every company that has ever carried this ticker.

    Returns a **list**, always, even when it holds one element. This is the
    enforcement: a caller cannot accidentally treat a ticker as a unique key,
    because the type does not let them. Share classes and recycled tickers are
    both real, and both produce more than one row here.

    If you want one company, resolve by CIK.
    """
    key = normalize_ticker(ticker)
    if key is None:
        return []
    rows = con.execute(
        "SELECT * FROM postgres_query('pg', ?)",
        [
            "select c.id, c.cik, ct.ticker, c.name, ct.source "
            "from company_tickers ct join companies c on c.id = ct.company_id "
            f"where ct.ticker = '{key}' order by ct.last_seen desc, c.id"
        ],
    ).fetchall()
    return [
        TickerMatch(
            company_id=int(r[0]), cik=r[1], ticker=r[2], name=r[3], source=r[4]
        )
        for r in rows
    ]


def is_ambiguous(matches: list[TickerMatch]) -> bool:
    """True when a ticker maps to more than one distinct company."""
    return len({m.company_id for m in matches}) > 1
