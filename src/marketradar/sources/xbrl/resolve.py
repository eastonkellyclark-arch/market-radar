"""Resolve the six concepts out of one quarter, and say what did not resolve.

One row per ``(accession, concept)``. **Long, not wide**, and that is the whole
design rather than a storage preference: individually these concepts clear 98%,
but all six on one filer is 64%. A wide table reports that intersection and
hides which concept did the excluding, so a consumer asking for revenue and net
income would silently pay the coverage cost of four it never reads.

The accession (``adsh``) is the key, per the identifier rule in CLAUDE.md: SEC
assigns it, never reuses it, and it identifies *this filing* rather than this
company-as-of-now. The CIK rides along for joining to companies; the company
name is carried for display and is never a join key.

**The unresolved are the deliverable too.** A concept that resolves for 51% of
filers and one that resolves for 99% are both just a number in a column
otherwise, which is the funnel rule applied to a normalizer. So every filing
gets a row for every concept, and the row says which of six states it is in --
see ``tag_map.STATUSES``. Three of those cost nothing to fix, two are out of
scope, and exactly one (``unmapped``) is a work queue, which is why they are
counted apart rather than summed into "missing".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

import duckdb

from marketradar import manifest
from marketradar.freshness import assert_fresh
from marketradar.screens import funnel as funnel_mod
from marketradar.sources.xbrl import tag_map
from marketradar.sources.xbrl.download import fetch as fetch_quarter

log = logging.getLogger(__name__)

DATASET: Final[str] = "xbrl_fundamentals"
SOURCE: Final[str] = "sec_financial_statements"

#: v1 reads annual figures from 10-K only.
#:
#: Not a simplification that got left in: every named consumer -- deal
#: multiples, the DCF engine, comparable-deal size buckets -- wants an annual
#: figure, and a column holding an annual number for some filers and a
#: quarterly one for others is the ``TOT_PARTCP_BOY_CNT`` mistake with a
#: different source. 10-Qs are counted out by name in the funnel rather than
#: quietly absent, so the gap is a decision on the record.
FORMS: Final[tuple[str, ...]] = ("10-K",)

#: ``fp`` for the annual period. A 10-K carrying Q-something is a transition
#: filing and its "year" is not twelve months.
ANNUAL_FP: Final[str] = "FY"

#: Only the standard taxonomy. A filer's own extension namespace is by
#: definition not comparable across filers, which is the one thing this table
#: exists to be.
TAXONOMY: Final[str] = "us-gaap%"

#: Minimum resolved rows before a load is treated as real. A quarter holds
#: roughly 2,800 operating 10-Ks and six concepts each; 1,000 is low enough not
#: to fire on a thin quarter and high enough that a parse that silently matched
#: nothing cannot pass.
MIN_ROWS: Final[int] = 1_000

#: Backends this dataset may be published to. SEC data is public domain, so a
#: world-readable Release asset is correct -- the exact opposite of the rule for
#: anything Tiingo touched, which is why it is asserted rather than assumed.
PUBLIC_BACKENDS: Final[frozenset[str]] = frozenset({"github_release", "local"})


@dataclass(frozen=True, slots=True)
class Coverage:
    """One concept's resolution counts for a quarter."""

    concept: str
    #: Filings in the population, which is the same for every concept.
    population: int
    #: ``{status: count}`` over ``tag_map.STATUSES``.
    by_status: dict[str, int]
    #: ``{tag: count}`` for the tags that actually supplied a value.
    by_tag: dict[str, int]
    #: The tags seen on unresolved filings that the map does not carry, most
    #: frequent first. The work queue, as a list rather than an investigation.
    unmapped_tags: list[tuple[str, int]] = field(default_factory=list)

    @property
    def resolved(self) -> int:
        return self.by_status.get(tag_map.STATED, 0)

    @property
    def rate(self) -> float:
        return self.resolved / self.population if self.population else 0.0

    @property
    def drift(self) -> float | None:
        """How far this quarter sits from the coverage the map was built on.

        The map is hand-maintained and baseline churn between sampled years ran
        11-20%, so it rots quietly. This is the number that says so.
        """
        got = tag_map.CONCEPTS.get(self.concept)
        return None if got is None else self.rate - got.coverage_2024q1

    def as_dict(self) -> dict[str, Any]:
        return {
            "concept": self.concept,
            "population": self.population,
            "resolved": self.resolved,
            "rate": self.rate,
            "drift": self.drift,
            "by_status": dict(self.by_status),
            "by_tag": dict(self.by_tag),
            "unmapped_tags": list(self.unmapped_tags),
        }


@dataclass(frozen=True, slots=True)
class LoadResult:
    """What a quarter produced, and what it discarded getting there."""

    quarter: str
    rows: int
    filings: int
    coverage: list[Coverage]
    funnel: funnel_mod.Funnel
    target: Path | None = None
    observed: Any = None

    def lines(self) -> list[str]:
        """The funnel and the per-concept coverage, for the CLI and the log."""
        out = list(self.funnel.lines())
        out.append("")
        out.append(f"{self.quarter}: coverage by concept "
                   f"({self.filings:,} filings in the population)")
        width = max((len(c.concept) for c in self.coverage), default=0)
        for cov in self.coverage:
            bit = (f"  {cov.concept:<{width}}  {cov.resolved:>6,}  "
                   f"{cov.rate:>6.1%}")
            if cov.drift is not None:
                bit += f"  {cov.drift:+.1%} vs the map's 2024q1 figure"
            out.append(bit)
            extra = [f"{s} {cov.by_status[s]:,}" for s in tag_map.STATUSES
                     if s != tag_map.STATED and cov.by_status.get(s)]
            if extra:
                out.append(f"  {'':<{width}}  {', '.join(extra)}")
            if cov.unmapped_tags:
                shown = ", ".join(f"{t} ({n})" for t, n in cov.unmapped_tags[:3])
                out.append(f"  {'':<{width}}  tags to add: {shown}")
        return out


# --- the population -----------------------------------------------------


def _filings_sql(sub_path: str) -> str:
    """Every annual operating-company 10-K in the quarter, classified.

    SIC and era are decided here, in one place, so the concept resolution below
    never has to know what a bank is.
    """
    non_op = " ".join(
        f"when try_cast(sic as integer) between {lo} and {hi} then '{name}'"
        for lo, hi, name in tag_map.NON_OPERATING_SIC
    )
    return f"""
        select
            adsh,
            cik,
            name                                        as company,
            try_cast(sic as integer)                    as sic,
            form,
            fp,
            try_cast(fy as integer)                     as fy,
            strptime(period, '%Y%m%d')::date            as period_end,
            strptime(filed,  '%Y%m%d')::date            as filed,
            case
                when sic is null or sic = '' then '{tag_map.UNCLASSIFIED}'
                when try_cast(sic as integer) is null then '{tag_map.UNCLASSIFIED}'
                when try_cast(sic as integer) = {tag_map.REIT_SIC} then 'reit'
                {non_op}
                else '{tag_map.OPERATING}'
            end                                         as sic_class
        from read_csv('{sub_path}', delim='\t', header=true,
                      sample_size=-1, all_varchar=true)
    """


def _population(
    con: duckdb.DuckDBPyConnection, quarter: str, tables: dict[str, Path]
) -> tuple[int, list[tuple[str, int, str]]]:
    """Build ``pop`` and the funnel stages that got there."""
    con.execute(
        f"create or replace table all_subs as {_filings_sql(tables['sub'].as_posix())}"
    )

    def count(where: str) -> int:
        return int(con.execute(
            f"select count(*) from all_subs where {where}").fetchone()[0])

    forms = ", ".join(f"'{f}'" for f in FORMS)
    stages: list[tuple[str, int, str]] = [
        ("submissions", count("true"), f"every filing in {quarter}"),
        ("annual report",
         count(f"form in ({forms})"),
         "10-K only; a 10-Q is a different period, not a smaller year"),
        ("full fiscal year",
         count(f"form in ({forms}) and fp = '{ANNUAL_FP}'"),
         "fp=FY; a transition period is not twelve months"),
        ("classified by SIC",
         count(f"form in ({forms}) and fp = '{ANNUAL_FP}' "
               f"and sic_class <> '{tag_map.UNCLASSIFIED}'"),
         "no SIC is not an operating company -- it is unknown"),
        ("operating company",
         count(f"form in ({forms}) and fp = '{ANNUAL_FP}' "
               f"and sic_class = '{tag_map.OPERATING}'"),
         "banks, insurers, brokers and REITs are a different table"),
    ]

    con.execute(f"""
        create or replace table pop as
        select *, '{tag_map.POST_606}' as era from all_subs
        where form in ({forms}) and fp = '{ANNUAL_FP}'
          and sic_class = '{tag_map.OPERATING}'
          and period_end is not null
          -- The ASC 606 boundary, on the fiscal year's *start*: the day after
          -- the same date a year earlier. Computing it from period_end alone
          -- would put every December filer on the wrong side, which is half
          -- the market and would resolve cleanly either way.
          and (period_end - interval 12 month + interval 1 day)::date
              >= DATE '{tag_map.ERA_BOUNDARY.isoformat()}'
    """)
    filings = int(con.execute("select count(*) from pop").fetchone()[0])
    stages.append((
        "post-606 era", filings,
        "v1 is 2019 forward; the pre-606 map is deliberately empty",
    ))
    return filings, stages


# --- the facts ----------------------------------------------------------


def _facts(
    con: duckdb.DuckDBPyConnection, tables: dict[str, Path]
) -> None:
    """Every ``num`` row for the population, with the reasons it might not count.

    Kept unfiltered on purpose -- **including the rows with no value at all**.
    Filtering to the usable rows here would make every rejection look identical
    downstream, and which rejection it was is the difference between "add a tag
    to the map" and "this filer reports in Canadian dollars".

    The nil rows are the reason this matters most. A filer that tags
    ``RevenueFromContractWithCustomerExcludingAssessedTax`` for its own fiscal
    year and reports no amount has said, in the tag, that it has no revenue --
    which is stronger evidence of `absent` than inferring it from whatever the
    income statement opens with. Dropping those rows here made 19
    clinical-stage biotechs read as `unmapped` under a tag the map already
    carries, which is a work-queue item that does not exist.

    And the value stays NULL. A nil tag is not a reported zero, and writing one
    in would be the same mistake as inferring a split ratio from a price jump:
    fabricated data in a table other things trust.
    """
    con.execute(f"""
        create or replace table facts as
        select
            n.adsh,
            n.tag,
            try_cast(n.qtrs as integer)               as qtrs,
            strptime(n.ddate, '%Y%m%d')::date         as ddate,
            n.uom,
            try_cast(n.value as decimal(28,4))        as value,
            coalesce(n.coreg, '') <> ''               as is_coreg,
            coalesce(n.segments, '') <> ''            as is_dimensional,
            n.value is not null and n.value <> ''     as has_value
        from read_csv('{tables['num'].as_posix()}', delim='\t', header=true,
                      sample_size=-1, all_varchar=true) n
        join pop p using (adsh)
        where n.version like '{TAXONOMY}'
    """)


def _toplines(con: duckdb.DuckDBPyConnection, tables: dict[str, Path]) -> None:
    """The tag each filer put at the top of its income statement.

    This is what separates ``absent`` from ``unmapped`` for revenue: a filer
    whose income statement opens with research and development expense is
    pre-revenue and there is nothing to find, while one opening with a revenue
    line we do not carry is a tag to add. 8.9% of operating filers are the
    former and 3.3% the latter, and summing them would describe a work queue
    three times its real size.

    ``inpth = '0'`` excludes parenthetical statements, and the ordering is
    (report, line, tag) -- an explicit key, so the same input always picks the
    same row.
    """
    con.execute(f"""
        create or replace table topline as
        with is_rows as (
            select
                p.adsh,
                pre.tag,
                try_cast(pre.report as integer) as report,
                try_cast(pre.line as integer)   as line
            from read_csv('{tables['pre'].as_posix()}', delim='\t', header=true,
                          sample_size=-1, all_varchar=true) pre
            join pop p using (adsh)
            where pre.stmt = 'IS' and pre.inpth = '0'
        )
        select adsh, min_by(tag, (report, line, tag)) as tag
        from is_rows
        where report is not null and line is not null
        group by adsh
    """)


# --- resolution ---------------------------------------------------------


def _resolve_concept(
    con: duckdb.DuckDBPyConnection, name: str, era: str = tag_map.POST_606
) -> None:
    """One concept, resolved for every filing in ``pop``, into ``resolved_<name>``.

    The winner is ``min_by`` on an explicit rank from the tag map -- never a
    function that picks a row from a group without saying which one. For net
    income that is not pedantry: 1,280 of 2,804 filers report two candidate
    tags and 616 report *different values* under them, so an arbitrary pick
    would make the column's meaning depend on scan order and three loads of
    identical input could disagree.
    """
    got = tag_map.concept(name, era)
    if got is None:
        raise ValueError(
            f"{name!r} has no map for era {era!r}. The pre-606 map is empty on "
            "purpose -- see tag_map.TAGS_BY_ERA."
        )
    ranks = " ".join(f"when '{tag}' then {i}" for i, tag in enumerate(got.tags))
    mapped = ", ".join(f"'{tag}'" for tag in got.tags)
    # Only revenue has evidence that tells `unmapped` from `absent`, and the
    # evidence is concept-specific on purpose -- see Concept.unmapped_when.
    # Without it, an unresolved filing is `absent`: the filer stated no total,
    # and there is no tag to go and find.
    if got.unmapped_when is None:
        unmapped_test, unmapped_tag = "false", "null"
    else:
        unmapped_test = (
            f"t.tag is not null and regexp_matches("
            f"t.tag, '{got.unmapped_when.pattern}', 'i')"
        )
        unmapped_tag = "t.tag"
    con.execute(f"""
        create or replace table resolved_{name} as
        with
        -- Every row for a mapped tag at **any** duration and date. The
        -- duration filter belongs in `usable`, not here: filtering it out this
        -- early threw away the evidence that a tag *is* reported, and 35
        -- filings then read as `unmapped` naming tags the map already carries.
        -- A work queue that lists things already done is worse than an empty
        -- one, because it reads as work.
        candidates as (
            select f.*, case f.tag {ranks} end as rank
            from facts f
            where f.tag in ({mapped})
        ),
        -- Consolidated and in USD, flagged for whether the row is this
        -- filing's own period and whether it carries an amount at all.
        consolidated as (
            select c.*, c.qtrs = {got.qtrs} and c.ddate = p.period_end
                        as own_period
            from candidates c join pop p using (adsh)
            where not c.is_coreg and not c.is_dimensional and c.uom = 'USD'
        ),
        usable as (
            select adsh, min_by(tag, (rank, tag)) as tag,
                   min_by(value, (rank, tag)) as value
            from consolidated
            where own_period and has_value
            group by adsh
        ),
        -- Why a filing with no usable row has none, in order of what a reader
        -- would do about it. Consolidated-and-USD-but-wrong-period first,
        -- because then the tag is reported and nothing in the map needs
        -- touching; a dimensional-only total is a question about aggregating
        -- segments; a Canadian reporter is out of scope for a USD table. Three
        -- different answers, and summing them into "missing" would describe
        -- none of them.
        excuses as (
            select adsh,
                   bool_or(own_period and not has_value)     as nil_at_period,
                   bool_or(not own_period and has_value)     as other_period
            from consolidated group by adsh
        ),
        other_axes as (
            select adsh,
                   bool_or(not is_coreg and is_dimensional
                           and has_value)                    as dimensional,
                   bool_or(not is_coreg and not is_dimensional
                           and uom <> 'USD' and has_value)    as foreign_ccy
            from candidates group by adsh
        )
        select
            p.adsh,
            u.tag,
            u.value,
            case
                when u.adsh is not null then '{tag_map.STATED}'
                -- Nil first: it is the filer's own statement about *this*
                -- period, and it outranks evidence about other periods or
                -- other axes.
                when e.nil_at_period then '{tag_map.ABSENT}'
                when e.other_period then '{tag_map.PERIOD_MISMATCH}'
                when x.dimensional then '{tag_map.SEGMENT_ONLY}'
                when x.foreign_ccy then '{tag_map.NOT_USD}'
                when {unmapped_test} then '{tag_map.UNMAPPED}'
                else '{tag_map.ABSENT}'
            end                                             as status,
            {unmapped_tag}                                  as unmapped_tag
        from pop p
        left join usable     u using (adsh)
        left join excuses    e using (adsh)
        left join other_axes x using (adsh)
        left join topline    t using (adsh)
    """)


def _coverage(
    con: duckdb.DuckDBPyConnection, name: str, population: int
) -> Coverage:
    by_status = {
        row[0]: int(row[1]) for row in con.execute(
            f"select status, count(*) from resolved_{name} group by 1"
        ).fetchall()
    }
    by_tag = {
        row[0]: int(row[1]) for row in con.execute(
            f"select tag, count(*) from resolved_{name} "
            "where tag is not null group by 1 order by 2 desc"
        ).fetchall()
    }
    unmapped = [
        (row[0], int(row[1])) for row in con.execute(
            f"select unmapped_tag, count(*) from resolved_{name} "
            f"where status = '{tag_map.UNMAPPED}' and unmapped_tag is not null "
            "group by 1 order by 2 desc, 1"
        ).fetchall()
    ]
    return Coverage(
        concept=name,
        population=population,
        by_status={s: by_status.get(s, 0) for s in tag_map.STATUSES},
        by_tag=by_tag,
        unmapped_tags=unmapped,
    )


# --- the load ------------------------------------------------------------


def build(
    quarter: str,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    cache: Path | None = None,
    concepts: tuple[str, ...] | None = None,
    tables: dict[str, Path] | None = None,
) -> tuple[duckdb.DuckDBPyRelation, LoadResult]:
    """Resolve one quarter and return ``(rows, result)`` without writing anything.

    ``concepts`` defaults to all six and is a parameter because that is the
    point of the long shape: asking for revenue alone should cost the coverage
    of revenue alone. Split out from :func:`load` so the numbers can be read --
    by a test, by the dashboard, or by a person deciding whether the map needs
    a tag -- without a publish.
    """
    con = con or duckdb.connect()
    if tables is None:
        tables = fetch_quarter(quarter, cache=cache).tables
    wanted = concepts or tuple(tag_map.CONCEPTS)
    for name in wanted:
        if name not in tag_map.CONCEPTS:
            raise ValueError(
                f"{name!r} is not in v1. Six concepts are mapped; "
                f"{sorted(tag_map.DEFERRED_CONCEPTS)} were measured and "
                "deferred, each to come back with its own coverage number."
            )

    filings, stages = _population(con, quarter, tables)
    _facts(con, tables)
    _toplines(con, tables)

    coverage: list[Coverage] = []
    unions: list[str] = []
    for name in wanted:
        _resolve_concept(con, name)
        coverage.append(_coverage(con, name, filings))
        unions.append(f"""
            select p.adsh, p.cik, p.company, p.sic, p.sic_class, p.era,
                   p.fy, p.period_end, p.filed,
                   '{name}' as concept, r.tag, r.value, r.status,
                   r.unmapped_tag, '{quarter}' as src_quarter,
                   '{SOURCE}' as source
            from resolved_{name} r join pop p using (adsh)
        """)

    con.execute(
        "create or replace table fundamentals as "
        + " union all by name ".join(unions)
    )
    rows = con.table("fundamentals")
    resolved = int(con.execute(
        f"select count(*) from fundamentals where status = '{tag_map.STATED}'"
    ).fetchone()[0])

    stages.append((
        "concept rows", int(con.execute(
            "select count(*) from fundamentals").fetchone()[0]),
        f"{len(wanted)} concepts x {filings:,} filings; every one gets a row",
    ))
    stages.append((
        "resolved values", resolved,
        "the rest carry a status saying which kind of miss it was",
    ))
    result = LoadResult(
        quarter=quarter,
        rows=resolved,
        filings=filings,
        coverage=coverage,
        funnel=funnel_mod.build(f"{DATASET}/{quarter}", *stages),
    )
    # Debug, not info: the CLI prints these and the two together double every
    # line. The log is the fallback for a caller that does not print.
    for line in result.lines():
        log.debug("%s", line)
    return rows, result


def load(
    quarter: str,
    out_dir: Path,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    cache: Path | None = None,
    concepts: tuple[str, ...] | None = None,
    tables: dict[str, Path] | None = None,
    min_rows: int = MIN_ROWS,
) -> LoadResult:
    """Resolve a quarter, write its parquet, and assert the result is real.

    Written locally and left for a separate, deliberate upload, the same as
    ``form5500.publish``: the partition is a GitHub Release asset because SEC
    data is public domain and ours to republish -- unlike anything Tiingo
    touched. See the licensing boundary in CLAUDE.md.
    """
    con = con or duckdb.connect()
    _, result = build(quarter, con=con, cache=cache, concepts=concepts,
                      tables=tables)

    ref = manifest.get(DATASET, quarter)
    if ref.backend not in PUBLIC_BACKENDS:
        raise ValueError(
            f"{DATASET}/{quarter} resolves to backend {ref.backend!r}. SEC data "
            "is public domain and belongs in a GitHub Release; a private "
            "backend here would mean the licensing boundary had been crossed "
            "in the wrong direction, which is the one mistake the manifest's "
            "backend column exists to make visible."
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{DATASET}_{quarter}.parquet"
    con.execute(
        f"copy (select * from fundamentals order by adsh, concept) "
        f"to '{dest.as_posix()}' (format parquet)"
    )
    # Read back from the file rather than asserting on the in-memory table:
    # the thing a consumer will open is the parquet, and anything that went
    # wrong in the write is invisible to an assertion on what went into it.
    con.execute(
        "create or replace view published as "
        f"select * from read_parquet('{dest.as_posix()}')"
    )
    rel = con.table("published")

    # Explicit, and the last statement that matters, per the hard rule in
    # CLAUDE.md.
    #
    # Asserted on the *resolved* rows, not on every concept row. A quarter
    # where the tag map matched nothing at all would still emit six rows per
    # filing, every one of them carrying a status, and a row count over the lot
    # would pass -- which is precisely the exit-green-on-empty-data failure
    # this assertion exists to prevent, dressed up as a full table.
    con.execute(
        "create or replace view published_values as "
        f"select * from published where status = '{tag_map.STATED}'"
    )
    observed = assert_fresh(
        DATASET,
        con.table("published_values"),
        partition=quarter,
        min_rows=min_rows,
        # A quarter's newest fiscal year end is a year or more old by the time
        # the data set ships, and none of that is staleness. The span is
        # measured below and put back on the observation instead, so
        # dataset_stats does not record NULL for every historical partition.
        date_column=None,
        expect_cols=("adsh", "cik", "concept", "value", "tag", "status",
                     "period_end", "era"),
    )
    newest = con.execute(
        "select max(period_end) from published_values").fetchone()[0]
    observed = replace(observed, max_date=newest)
    manifest.record_stats(observed, con=con)
    log.info("wrote %s", dest)
    return replace(result, target=dest, observed=observed)
