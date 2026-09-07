"""Coverage between the SEC filer list and the Tiingo sweep universe.

The raw counts are easy and misleading. Half the Tiingo universe has no SEC
CIK and a quarter of SEC filers are not swept, which reads as an entity crisis
and is mostly not one: it is ETFs, warrants, and OTC symbols our own universe
filter drops. This module exists to separate *category mismatch* from *real
identity ambiguity*, because only the second kind costs manual work.

The two directions are counted in different units on purpose.

Tiingo -> SEC is counted in **tickers**: the nightly sweep is per-ticker, so
an unmatched ticker is one symbol whose filings we cannot reach.

SEC -> Tiingo is counted in **CIKs**. A filer is the entity. A CIK carrying
eleven preferred share classes is one company to resolve by hand, not eleven,
and counting its tickers roughly doubles the apparent backlog. A CIK counts as
covered when *any* of its tickers is in the universe.

The classification below is heuristic and says so at each step. The one split
that is *not* heuristic is the last and most important: whether Tiingo carries
a symbol at all, which comes from Tiingo's own ``exchange`` field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final, Iterable, Iterator, Mapping

#: Trailing markers for units, warrants, and rights. A five-plus character
#: symbol ending in U/W/R is nearly always a SPAC unit, warrant, or right, and
#: those are *instruments of* a filer rather than filers, so they never carry
#: their own CIK. Heuristic: a genuine five-letter symbol ending in one of
#: those letters is misfiled here. Accepted, because the alternative is
#: calling several hundred SPAC warrants "unidentified companies".
_STRUCTURED_TAIL: Final[re.Pattern[str]] = re.compile(
    r"(?:WS|WT|RT)$|(?<=[A-Z]{4})[UWR]$"
)

#: Four letters plus F or Y: foreign ordinary shares and ADRs. These are real
#: filers, but they trade OTC, so the listed-only universe never sees them.
_FOREIGN: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{4}[FY]$")

#: Exchange values in Tiingo's file that are OTC venues rather than listings.
#: Membership here is what makes "absent from our universe" explainable as a
#: filter decision rather than missing data.
OTC_EXCHANGES: Final[frozenset[str]] = frozenset(
    {"PINK", "OTCMKTS", "OTCQB", "OTCQX", "OTCGREY", "OTCD", "NMFQS", "OTCBB"}
)


def ticker_variants(ticker: str) -> set[str]:
    """Spellings of one ticker that different vendors treat as the same symbol.

    SEC writes ``BRK-B`` where Tiingo sometimes writes ``BRK.B``, and a bare
    string compare scores those as two different companies. Used for
    *diagnosis only* — it separates "we do not know who this is" from "we
    spell it differently", which are very different amounts of work. It is
    never a join key, and nothing here may be used to resolve an entity.
    """
    out = {ticker}
    for a, b in (("-", "."), (".", "-")):
        if a in ticker:
            out.add(ticker.replace(a, b))
    out.add(ticker.replace("-", "").replace(".", ""))
    return out


def is_structured(ticker: str) -> bool:
    """True for units, warrants, rights, and preferred-class notation.

    A separator is the strong signal: ``AAC-U``, ``CTA-PA``, ``T-P-C`` are all
    instruments written against a parent symbol.
    """
    return "-" in ticker or "." in ticker or bool(_STRUCTURED_TAIL.search(ticker))


def base_symbol(ticker: str) -> str:
    """The parent symbol a structured ticker hangs off, best effort."""
    base = re.sub(r"[-.].*$", "", ticker)
    return re.sub(r"(?:WS|WT|RT)$|(?<=[A-Z]{4})[UWR]$", "", base)


@dataclass(frozen=True, slots=True)
class TiingoSide:
    """Tiingo tickers with no SEC CIK, decomposed. Counted in tickers."""

    universe: int
    matched: int
    unmatched: int
    etfs: int
    structured: int
    structured_parent_known: int
    structured_parent_unknown: int
    unidentified: int
    by_asset_type: dict[str, int] = field(default_factory=dict)
    by_exchange: dict[str, int] = field(default_factory=dict)
    sample: list[tuple[str, str, str]] = field(default_factory=list)
    sample_unidentified: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SecSide:
    """SEC CIKs with no Tiingo ticker, decomposed. Counted in CIKs."""

    filers: int
    covered: int
    uncovered: int
    filter_dropped: int
    absent: int
    foreign: int
    dropped_by_exchange: dict[str, int] = field(default_factory=dict)
    sample_uncovered: list[tuple[str, str, str]] = field(default_factory=list)
    sample_absent: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Spelling:
    """How much of the gap is punctuation rather than identity."""

    ticker_hits: int
    cik_hits: int
    sample: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Reconciliation:
    pairs: int
    tiingo: TiingoSide
    sec: SecSide
    spelling: Spelling

    @property
    def real_gap_tickers(self) -> int:
        """Unmatched Tiingo tickers that are neither ETF, instrument, nor typo."""
        return self.tiingo.unidentified

    @property
    def real_gap_ciks(self) -> int:
        """Uncovered CIKs Tiingo genuinely does not carry."""
        return self.sec.absent


def build(
    filers: Iterable[Any],
    active: Iterable[Any],
    raw_rows: Iterable[Mapping[str, Any]],
    *,
    sample_size: int = 15,
) -> Reconciliation:
    """Compare the two universes.

    Args:
        filers: SEC ``Filer`` records — anything with ``.cik``, ``.ticker``,
            ``.name``.
        active: the filtered sweep universe — ``TickerMeta`` records.
        raw_rows: every row of Tiingo's supported-ticker file, *unfiltered*.
            This is what makes the last split exact: without it, a symbol our
            own filter drops is indistinguishable from one Tiingo lacks.
    """
    filers = list(filers)
    meta = {t.ticker: t for t in active}
    tiingo_tickers = set(meta)

    raw: dict[str, Mapping[str, Any]] = {}
    for row in raw_rows:
        symbol = (row.get("ticker") or "").strip().upper()
        if symbol:
            raw.setdefault(symbol, row)

    sec_tickers = {f.ticker for f in filers}
    by_cik: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    for f in filers:
        by_cik.setdefault(f.cik, set()).add(f.ticker)
        names.setdefault(f.cik, f.name)

    # --- direction 1: Tiingo -> SEC, in tickers --------------------------
    only_tiingo = tiingo_tickers - sec_tickers
    matched = tiingo_tickers & sec_tickers

    etfs = {t for t in only_tiingo if meta[t].asset_type == "etf"}
    stocks = only_tiingo - etfs
    structured = {t for t in stocks if is_structured(t)}
    # A warrant whose parent is a filer we already hold is zero manual work:
    # we know whose instrument it is. One whose parent is also unknown is a
    # thread to pull. Splitting them is the difference that matters.
    parent_known = {t for t in structured if base_symbol(t) in sec_tickers}
    unidentified = stocks - structured

    by_type: dict[str, int] = {}
    by_exch: dict[str, int] = {}
    for t in only_tiingo:
        m = meta[t]
        by_type[m.asset_type] = by_type.get(m.asset_type, 0) + 1
        by_exch[m.exchange] = by_exch.get(m.exchange, 0) + 1

    tiingo_side = TiingoSide(
        universe=len(tiingo_tickers),
        matched=len(matched),
        unmatched=len(only_tiingo),
        etfs=len(etfs),
        structured=len(structured),
        structured_parent_known=len(parent_known),
        structured_parent_unknown=len(structured - parent_known),
        unidentified=len(unidentified),
        by_asset_type=by_type,
        by_exchange=by_exch,
        sample=[
            (t, meta[t].exchange, meta[t].asset_type)
            for t in sorted(only_tiingo)[:sample_size]
        ],
        sample_unidentified=[
            (t, meta[t].exchange, str(meta[t].end_date))
            for t in sorted(unidentified)[:sample_size]
        ],
    )

    # --- direction 2: SEC -> Tiingo, in CIKs -----------------------------
    uncovered = {c for c, tks in by_cik.items() if not (tks & tiingo_tickers)}
    dropped, absent = set(), set()
    dropped_by_exchange: dict[str, int] = {}
    for cik in uncovered:
        present = [raw[t] for t in by_cik[cik] if t in raw]
        if present:
            dropped.add(cik)
            for row in present:
                exch = (row.get("exchange") or "").strip().upper() or "(blank)"
                dropped_by_exchange[exch] = dropped_by_exchange.get(exch, 0) + 1
        else:
            absent.add(cik)

    foreign = {c for c in absent if all(_FOREIGN.match(t) for t in by_cik[c])}

    def _rows(ciks: Iterable[str]) -> list[tuple[str, str, str]]:
        return [
            (c, ",".join(sorted(by_cik[c]))[:26], names[c][:44])
            for c in sorted(ciks)[:sample_size]
        ]

    sec_side = SecSide(
        filers=len(by_cik),
        covered=len(by_cik) - len(uncovered),
        uncovered=len(uncovered),
        filter_dropped=len(dropped),
        absent=len(absent),
        foreign=len(foreign),
        dropped_by_exchange=dropped_by_exchange,
        sample_uncovered=_rows(uncovered),
        sample_absent=_rows(absent),
    )

    # --- spelling, not identity ------------------------------------------
    variant_index: dict[str, str] = {}
    for t in sec_tickers:
        for v in ticker_variants(t):
            variant_index.setdefault(v, t)
    ticker_hits = {t for t in only_tiingo if ticker_variants(t) & sec_tickers}
    cik_hits = {
        c for c in uncovered
        if any(ticker_variants(t) & tiingo_tickers for t in by_cik[c])
    }
    spelling = Spelling(
        ticker_hits=len(ticker_hits),
        cik_hits=len(cik_hits),
        sample=[
            (
                t,
                next(
                    (variant_index[v] for v in ticker_variants(t) if v in variant_index),
                    "?",
                ),
            )
            for t in sorted(ticker_hits)[:8]
        ],
    )

    return Reconciliation(
        pairs=len(filers), tiingo=tiingo_side, sec=sec_side, spelling=spelling
    )


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "n/a"


def render(rep: Reconciliation) -> Iterator[str]:
    """The report, as lines. Separated from printing so tests can read it."""
    t, s, sp = rep.tiingo, rep.sec, rep.spelling

    yield ""
    yield f"Tiingo universe (active listed) : {t.universe:,}"
    yield f"SEC filers (distinct CIKs)      : {s.filers:,}"
    yield f"SEC (cik, ticker) pairs         : {rep.pairs:,}"

    yield ""
    yield "--- direction 1: Tiingo tickers with no SEC CIK (counted in tickers) ---"
    yield f"  matched on ticker             : {t.matched:,} ({_pct(t.matched, t.universe)} of Tiingo)"
    yield f"  unmatched                     : {t.unmatched:,} ({_pct(t.unmatched, t.universe)})"
    yield ""
    yield "  decomposed:"
    yield f"    ETFs (no operating CIK)     : {t.etfs:,}"
    yield f"    units / warrants / rights   : {t.structured:,}"
    yield f"      parent is a known filer   : {t.structured_parent_known:,}"
    yield f"      parent also unknown       : {t.structured_parent_unknown:,}"
    yield f"    -> genuinely unidentified   : {t.unidentified:,}"

    if t.by_asset_type:
        yield ""
        yield "  unmatched by asset type:"
        for k, v in sorted(t.by_asset_type.items(), key=lambda kv: -kv[1]):
            yield f"    {k:<10} {v:>6,}"
    if t.by_exchange:
        yield "  unmatched by exchange:"
        for k, v in sorted(t.by_exchange.items(), key=lambda kv: -kv[1])[:6]:
            yield f"    {k:<10} {v:>6,}"
    if t.sample_unidentified:
        yield ""
        yield "  sample genuinely-unidentified (ticker / exchange / last traded):"
        for sym, exch, end in t.sample_unidentified:
            yield f"    {sym:<12} {exch:<10} {end}"

    yield ""
    yield "--- direction 2: SEC CIKs with no Tiingo ticker (counted in CIKs) ---"
    yield f"  covered CIKs                  : {s.covered:,} ({_pct(s.covered, s.filers)} of filers)"
    yield f"  uncovered CIKs                : {s.uncovered:,} ({_pct(s.uncovered, s.filers)})"
    yield ""
    yield "  decomposed:"
    yield f"    Tiingo has it, our filter drops it : {s.filter_dropped:,}"
    yield f"    Tiingo does not carry it at all    : {s.absent:,}"
    yield f"      of those, foreign ordinary/ADR   : {s.foreign:,}"

    if s.dropped_by_exchange:
        yield ""
        yield "  exchange of the symbols our filter drops:"
        for k, v in sorted(s.dropped_by_exchange.items(), key=lambda kv: -kv[1])[:10]:
            tag = "  (OTC)" if k in OTC_EXCHANGES else ""
            yield f"    {k:<12} {v:>6,}{tag}"
    if s.sample_absent:
        yield ""
        yield "  sample Tiingo genuinely does not carry (cik / tickers / name):"
        for cik, tks, name in s.sample_absent:
            yield f"    {cik}  {tks:<28} {name}"

    yield ""
    yield "--- how much of the gap is spelling, not identity ---"
    yield f"  unmatched Tiingo tickers hitting a SEC ticker under a variant : {sp.ticker_hits:,}"
    yield f"  uncovered CIKs hitting the universe under a variant           : {sp.cik_hits:,}"
    if sp.sample:
        yield ""
        yield "  sample separator collisions (tiingo -> sec):"
        for a, b in sp.sample:
            yield f"    {a:<12} -> {b}"

    yield ""
    yield f"real identity gap, Tiingo side : {rep.real_gap_tickers:,} tickers"
    yield f"real identity gap, SEC side    : {rep.real_gap_ciks:,} CIKs"
