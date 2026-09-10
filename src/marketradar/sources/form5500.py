"""DOL Form 5500 — private company sourcing, keyed on EIN.

**Two files per plan year, not one.** ``F_5500`` is the main form (plans with
100+ participants, 225,591 filings for 2024) and ``F_5500_SF`` is the short
form (under 100 participants, 798,006 filings). The short form is three and a
half times the main one and is where small private employers are, so a loader
that reads only the main form has a quarter of the data and the wrong quarter.

**Resolution keys on EIN, never on the sponsor name.** Measured 2026-09-10
across all 1,023,597 filings for plan year 2024:

- EIN is present on **100%** of them. Not 99% -- zero missing.
- Of the 22,141 sponsors with an authoritative EIN match to an SEC filer,
  exact-name matching finds 39.3% and normalized-name matching 83.6%.
- But normalized-name matching also produced 23,406 matches whose EIN
  disagreed, so its **best-case precision is 44.2%** -- wrong more often than
  right. Eleven SEC filers share the normalized key ``'energy'``.

So there is no fuzzy matching here. A matcher wrong more than half the time
is worse than no matcher, because the errors are invisible. Names that match
while EINs disagree become ``entity_review`` rows: a question for a human,
not a merge. See the identifier rule in CLAUDE.md.

**DFE filings are flagged, not dropped.** 9,805 of the main form's filings
are Direct Filing Entities -- master trusts, collective investment funds,
pooled separate accounts. They are trustees rather than employers and they
dominate any plan-count-weighted view: sorted by plans, the top of the
"private" population is Transamerica Life, State Street Global Advisors Trust
and BNY Mellon. Marked as a category and excluded from employer lists, the
same treatment as the FUND flag on Form 4 clusters and the SPAC deal type.

**Partial plan years are visible, never silently thin.** Filings lag the plan
year: 2026 does not exist yet and 2025 is a third the size of 2024 because it
is still being filed. :func:`completeness` says so rather than letting a
small count read as a small year.
"""

from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final, Iterable

import duckdb

from marketradar import manifest, storage
from marketradar.freshness import assert_fresh

log = logging.getLogger(__name__)

DATASET: Final[str] = "form5500_sponsors"
SOURCE: Final[str] = "dol_efast"

#: Newest plan year with complete filings. Filings lag the plan year by about
#: eighteen months, so this is not "last year" and moves once a year.
NEWEST_COMPLETE_YEAR: Final[int] = 2024

#: Below this share of the newest complete year's filings, a plan year is
#: reported as still being filed rather than as a real decline. 2025 sat at
#: about a third of 2024 when this was written.
PARTIAL_YEAR_RATIO: Final[float] = 0.85

#: Direct Filing Entity codes from ``TYPE_DFE_PLAN_ENTITY_CD``. A filing
#: carrying one is a pooled vehicle, not an employer.
DFE_CODES: Final[dict[str, str]] = {
    "M": "master trust investment account",
    "C": "collective investment fund",
    "P": "pooled separate account",
    "E": "103-12 investment entity",
    "G": "group insurance arrangement",
    "D": "direct filing entity",
}

#: Column names differ between the two forms for the same field, so each is
#: mapped to one shape rather than branching downstream.
_MAIN_COLS: Final[dict[str, str]] = {
    "name": "SPONSOR_DFE_NAME",
    "dba": "SPONS_DFE_DBA_NAME",
    "ein": "SPONS_DFE_EIN",
    "naics": "BUSINESS_CODE",
    "city": "SPONS_DFE_MAIL_US_CITY",
    "state": "SPONS_DFE_MAIL_US_STATE",
    "zip": "SPONS_DFE_MAIL_US_ZIP",
    "plan_name": "PLAN_NAME",
    "participants": "TOT_PARTCP_BOY_CNT",
    "dfe": "TYPE_DFE_PLAN_ENTITY_CD",
}
_SF_COLS: Final[dict[str, str]] = {
    "name": "SF_SPONSOR_NAME",
    "dba": "SF_SPONSOR_DFE_DBA_NAME",
    "ein": "SF_SPONS_EIN",
    "naics": "SF_BUSINESS_CODE",
    "city": "SF_SPONS_US_CITY",
    "state": "SF_SPONS_US_STATE",
    "zip": "SF_SPONS_US_ZIP",
    "plan_name": "SF_PLAN_NAME",
    "participants": "SF_TOT_PARTCP_BOY_CNT",
    "dfe": None,          # the short form has no DFE concept
}

#: Corporate suffixes and filler dropped before a name comparison. Only ever
#: used for the review queue and for display -- never as a join key.
NAME_NOISE: Final[tuple[str, ...]] = (
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies",
    "llc", "llp", "lllp", "lp", "ltd", "limited", "plc", "pc", "pa", "psc",
    "holdings", "holding", "group", "the", "and", "of", "a", "an",
)


class Form5500Error(RuntimeError):
    """A Form 5500 dataset could not be read."""


def normalize_sql(column: str) -> str:
    """Vectorised name normalisation, as SQL.

    A Python UDF over 1.86 million rows had not finished in seventy minutes;
    the same transformation in SQL runs in seconds. ``trust`` and
    ``employees`` are deliberately *not* stripped -- for a plan sponsor those
    words carry meaning, and folding them away was part of why name matching
    scores 44.2%.
    """
    drop = ", ".join(f"'{w}'" for w in NAME_NOISE)
    return f"""
        array_to_string(
            list_filter(
                string_split_regex(
                    trim(regexp_replace(
                        lower(replace({column}, '&', ' and ')),
                        '[^a-z0-9 ]', ' ', 'g')),
                    '\\s+'),
                w -> w <> '' and w not in ({drop})
            ), ' ')
    """


# --- reading the archives ------------------------------------------------


def dataset_url(kind: str, year: int) -> str:
    """The DOL URL for one file. Resolved through the manifest."""
    if kind not in ("main", "short"):
        raise Form5500Error(f"unknown Form 5500 file kind {kind!r}")
    return manifest.get(SOURCE, kind).location.replace("{year}", str(year))


@dataclass(frozen=True, slots=True)
class Archive:
    """One downloaded zip and the CSV inside it."""

    kind: str
    year: int
    path: Path

    @property
    def member(self) -> str:
        stem = "f_5500_sf" if self.kind == "short" else "f_5500"
        return f"{stem}_{self.year}_latest.csv"

    def extract(self, into: Path) -> Path:
        """Unpack the CSV so DuckDB can read it natively.

        DuckDB cannot read inside a zip, and going through Python's csv
        module instead cost minutes per file on a million rows.
        """
        into.mkdir(parents=True, exist_ok=True)
        dest = into / self.member
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        try:
            with zipfile.ZipFile(self.path) as z, z.open(self.member) as src:
                with dest.open("wb") as out:
                    while chunk := src.read(1 << 22):
                        out.write(chunk)
        except (KeyError, zipfile.BadZipFile) as exc:
            raise Form5500Error(
                f"{self.path.name} does not contain {self.member}: {exc}"
            ) from exc
        return dest


def fetch(
    year: int,
    cache: Path,
    *,
    kinds: Iterable[str] = ("main", "short"),
    client: Any = None,
) -> list[Archive]:
    """Download both files for one plan year. Cached by content length."""
    import httpx

    cache.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "market-radar/0.1 (personal research)"}
    owns = client is None
    client = client or httpx.Client(timeout=300.0, follow_redirects=True,
                                    headers=headers)
    out: list[Archive] = []
    try:
        for kind in kinds:
            url = dataset_url(kind, year)
            name = url.rsplit("/", 1)[1]
            dest = cache / name
            head = client.head(url)
            if head.status_code != 200:
                raise Form5500Error(
                    f"{name}: HTTP {head.status_code}. Plan year {year} may "
                    "not be published yet -- filings lag the plan year by "
                    "about eighteen months."
                )
            size = int(head.headers.get("content-length") or 0)
            if not (dest.exists() and dest.stat().st_size == size):
                with client.stream("GET", url) as r:
                    r.raise_for_status()
                    tmp = dest.with_suffix(".part")
                    with tmp.open("wb") as fh:
                        for chunk in r.iter_bytes(1 << 20):
                            fh.write(chunk)
                    tmp.replace(dest)
            log.info("%s: %.1f MB (last-modified %s)", name, size / 1e6,
                     head.headers.get("last-modified"))
            out.append(Archive(kind=kind, year=year, path=dest))
    finally:
        if owns:
            client.close()
    return out


# --- shaping -------------------------------------------------------------


def _select(csv_path: Path, cols: dict[str, str | None], year: int,
            form: str) -> str:
    """One SELECT that normalises a form's columns into the common shape."""
    def col(key: str) -> str:
        name = cols.get(key)
        return f'trim("{name}")' if name else "cast(null as varchar)"

    dfe = cols.get("dfe")
    dfe_expr = f'upper(trim("{dfe}"))' if dfe else "cast(null as varchar)"
    return f"""
        select
            {year}                       as plan_year,
            '{form}'                     as form,
            regexp_replace(coalesce({col('ein')}, ''), '\\D', '', 'g')
                                         as ein_raw,
            {col('name')}                as sponsor_name,
            nullif({col('dba')}, '')     as dba_name,
            nullif({col('naics')}, '')   as naics,
            nullif({col('city')}, '')    as city,
            nullif({col('state')}, '')   as state,
            nullif({col('zip')}, '')     as zip,
            nullif({col('plan_name')}, '') as plan_name,
            try_cast({col('participants')} as bigint) as participants,
            nullif({dfe_expr}, '')       as dfe_code
        from read_csv_auto('{csv_path.as_posix()}', all_varchar=true,
                           ignore_errors=true)
    """


def load_filings(
    con: duckdb.DuckDBPyConnection,
    archives: Iterable[Archive],
    workdir: Path,
    *,
    table: str = "f5500_filings",
) -> int:
    """One row per filing, both forms merged into one shape."""
    parts = []
    year = None
    for archive in archives:
        year = archive.year
        csv_path = archive.extract(workdir)
        cols = _SF_COLS if archive.kind == "short" else _MAIN_COLS
        parts.append(_select(csv_path, cols, archive.year, archive.kind))
    if not parts:
        raise Form5500Error("no archives given")

    con.execute(f"drop table if exists {table}")
    con.execute(f"""
        create table {table} as
        select * exclude (ein_raw),
               case when length(ein_raw) = 9 and ein_raw <> '000000000'
                    then ein_raw end as ein
        from ({' union all by name '.join(parts)})
        where sponsor_name is not null and sponsor_name <> ''
    """)
    n = con.execute(f"select count(*) from {table}").fetchone()[0]
    missing = con.execute(
        f"select count(*) from {table} where ein is null").fetchone()[0]
    if missing:
        # Measured at zero across 1,023,597 filings, so this is a real change
        # in the source rather than an expected trickle.
        log.warning(
            "%d of %d Form 5500 filings for %s carry no usable EIN. EIN was "
            "present on 100%% of plan year 2024; resolution keys on it, so a "
            "filing without one cannot be resolved at all.",
            missing, n, year,
        )
    return int(n)


def build_sponsors(
    con: duckdb.DuckDBPyConnection,
    *,
    filings: str = "f5500_filings",
    table: str = "f5500_sponsors",
) -> int:
    """One row per (plan year, EIN). A company with a 401(k) and a cafeteria
    plan files twice and is one company.

    Participants are summed across the sponsor's plans and also carried as a
    max, because a sponsor with several plans double-counts the same people:
    the sum is an upper bound and the max a lower one, and neither is the
    headcount on its own.
    """
    con.execute(f"drop table if exists {table}")
    con.execute(f"""
        create table {table} as
        select
            plan_year,
            ein,
            -- min(), not any_value(). One EIN can carry several spellings
            -- across its plans, and any_value picks a different one per run:
            -- three consecutive loads produced 22,680, 22,685 and 22,686
            -- review rows from identical input, because the name chosen
            -- decides whether the sponsor name-matches at all. Jobs are
            -- idempotent (CLAUDE.md), so the choice has to be a rule.
            min(sponsor_name)                             as sponsor_name,
            max(dba_name)                                 as dba_name,
            max(naics)                                    as naics,
            max(city)                                     as city,
            max(state)                                    as state,
            max(zip)                                      as zip,
            count(*)                                      as plans,
            sum(coalesce(participants, 0))                as participants_sum,
            max(coalesce(participants, 0))                as participants_max,
            -- Flagged, never dropped: a DFE is a trustee, and mixing one into
            -- an employer list is how 'private companies' comes back as
            -- Transamerica Life and BNY Mellon.
            max(dfe_code)                                 as dfe_code,
            bool_or(dfe_code is not null)                 as is_dfe,
            bool_or(form = 'short')                       as files_short_form,
            bool_or(form = 'main')                        as files_main_form
        from {filings}
        where ein is not null
        group by plan_year, ein
    """)
    return int(con.execute(f"select count(*) from {table}").fetchone()[0])


# --- resolution ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resolution:
    """What resolving one plan year produced."""

    plan_year: int
    sponsors: int
    by_ein: int
    by_ein_listed: int
    name_only: int
    private: int
    dfe: int

    @property
    def private_share(self) -> float:
        return self.private / self.sponsors if self.sponsors else 0.0


def resolve(
    con: duckdb.DuckDBPyConnection,
    filers: duckdb.DuckDBPyRelation,
    *,
    sponsors: str = "f5500_sponsors",
    table: str = "f5500_resolved",
) -> Resolution:
    """Join sponsors to SEC filers on EIN, and score the names against it.

    ``filers`` needs ``cik``, ``ein``, ``name`` and ``tickers``.

    The name comparison exists to *populate the review queue*, not to
    resolve anything: every row it produces that the EIN join did not is a
    question, and 44.2% of them are wrong.
    """
    con.register("sec_filers", filers)
    con.execute(f"""
        create or replace table sec_keyed as
        select cik, ein, name, tickers,
               upper(trim(name)) as ekey,
               {normalize_sql("name")} as nkey
        from sec_filers
    """)
    con.execute("""
        create or replace table sec_by_ein as
        select ein,
               min(cik) as cik,
               min(name) as name,
               max(tickers) as tickers,
               count(*) as filers
        from sec_keyed where ein is not null and ein <> '' group by ein
    """)
    # Two lookups, each grouped on the single key it is joined by. Grouping
    # one table on (ekey, nkey) and then joining it on nkey alone fanned the
    # sponsor set from 858,480 rows to 866,175: one normalized key can span
    # several distinct exact names, so the join multiplied rows instead of
    # matching them. The unique constraint on entity_review hid it -- the
    # queue was right and every count was wrong.
    con.execute("""
        create or replace table sec_by_exact as
        select ekey, min(cik) as cik, min(name) as name,
               count(*) as candidates
        from sec_keyed group by ekey
    """)
    con.execute("""
        create or replace table sec_by_norm as
        select nkey, min(cik) as cik, min(name) as name,
               count(*) as candidates
        from sec_keyed where nkey <> '' group by nkey
    """)

    con.execute(f"drop table if exists {table}")
    con.execute(f"""
        create table {table} as
        with keyed as (
            select s.*,
                   upper(trim(s.sponsor_name)) as ekey,
                   {normalize_sql("s.sponsor_name")} as nkey
            from {sponsors} s
        )
        select k.*,
               e.cik                              as ein_cik,
               e.name                             as ein_name,
               coalesce(e.tickers, '')            as ein_tickers,
               nx.cik                             as exact_cik,
               nx.name                            as exact_name,
               nx.candidates                      as exact_candidates,
               nn.cik                             as norm_cik,
               nn.name                            as norm_name,
               nn.candidates                      as norm_candidates,
               e.ein is not null                  as by_ein,
               coalesce(e.tickers, '') <> ''      as by_ein_listed
        from keyed k
        left join sec_by_ein e on e.ein = k.ein
        left join sec_by_exact nx on nx.ekey = k.ekey
        left join sec_by_norm nn on nn.nkey = k.nkey and k.nkey <> ''
    """)

    expected = int(con.execute(
        f"select count(*) from {sponsors}").fetchone()[0])
    got = int(con.execute(f"select count(*) from {table}").fetchone()[0])
    if got != expected:
        raise Form5500Error(
            f"resolution changed the row count: {expected:,} sponsors in and "
            f"{got:,} out. A left join fanned out, which inflates every "
            "count downstream without failing anything."
        )

    row = con.execute(f"""
        select any_value(plan_year), count(*),
               count(*) filter (where by_ein),
               count(*) filter (where by_ein_listed),
               count(*) filter (where not by_ein
                                and (exact_cik is not null
                                     or norm_cik is not null)),
               count(*) filter (where not by_ein
                                and exact_cik is null and norm_cik is null),
               count(*) filter (where is_dfe)
        from {table}
    """).fetchone()
    return Resolution(
        plan_year=int(row[0]), sponsors=int(row[1]), by_ein=int(row[2]),
        by_ein_listed=int(row[3]), name_only=int(row[4]),
        private=int(row[5]), dfe=int(row[6]),
    )


def completeness(
    con: duckdb.DuckDBPyConnection,
    plan_year: int,
    filings: int,
    *,
    baseline: int | None = None,
) -> tuple[bool, str]:
    """(complete, why). A thin year must say it is young, not just small.

    Filings lag the plan year by about eighteen months, so a plan year read
    too early looks like an industry-wide collapse. 2025 held a third of
    2024's filings while it was still being filed.
    """
    if plan_year > NEWEST_COMPLETE_YEAR:
        if baseline and filings >= baseline * PARTIAL_YEAR_RATIO:
            return True, f"{filings:,} filings, at parity with {NEWEST_COMPLETE_YEAR}"
        share = f"{filings / baseline:.0%} of {NEWEST_COMPLETE_YEAR}" if baseline \
            else "no baseline to compare against"
        return False, (
            f"plan year {plan_year} is still being filed ({share}). "
            f"{NEWEST_COMPLETE_YEAR} is the newest complete year -- counts "
            "here are a filing lag, not a decline."
        )
    return True, f"{filings:,} filings, plan year complete"


# --- persistence ---------------------------------------------------------


def review_rows(
    con: duckdb.DuckDBPyConnection, *, table: str = "f5500_resolved"
) -> list[tuple]:
    """The review queue: name matched, EIN did not.

    Ordered so the useful rows come first: a name matching exactly and
    matching only one filer is a plausible subsidiary; a normalized name
    matching eleven filers is noise, and sorting by candidate count puts it
    last rather than deleting it.
    """
    return con.execute(f"""
        select ein, plan_year, sponsor_name,
               coalesce(exact_cik, norm_cik) as matched_cik,
               coalesce(exact_name, norm_name) as matched_name,
               case when exact_cik is not null then 'exact_name'
                    else 'normalized_name' end as match_basis,
               coalesce(exact_candidates, norm_candidates, 1) as candidates,
               naics, state,
               least(participants_sum, 2147483647) as participants
        from {table}
        where not by_ein and (exact_cik is not null or norm_cik is not null)
        order by (exact_cik is null), candidates, ein
    """).fetchall()


def load_review_queue(
    rows: Iterable[tuple], con: duckdb.DuckDBPyConnection | None = None,
    *, chunk: int = 500,
) -> dict[str, int]:
    """Upsert review candidates. Never touches a decided row.

    ``do nothing`` rather than ``do update``: a human has already answered
    some of these, and re-running the loader must not reopen a closed
    question.
    """
    rows = list(rows)
    con = con or storage.connect(attach_postgres=True)
    if not storage.postgres_attached(con):
        raise Form5500Error("No Postgres attached; cannot load the review queue.")

    def lit(v: Any) -> str:
        if v is None:
            return "null"
        if isinstance(v, (int, float)):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"

    def q(sql: str) -> list[tuple]:
        return con.execute("SELECT * FROM postgres_query('pg', ?)", [sql]).fetchall()

    before = q("select count(*) as n from entity_review")[0][0]
    for i in range(0, len(rows), chunk):
        values = ", ".join(
            "({})".format(", ".join(lit(v) for v in r)) for r in rows[i:i + chunk]
        )
        con.execute("CALL postgres_execute('pg', ?)", [
            "insert into entity_review (ein, plan_year, sponsor_name, "
            "matched_cik, matched_name, match_basis, candidates, naics, "
            f"state, participants) values {values} "
            "on conflict (ein, plan_year, match_basis) do nothing"
        ])
    after = q("select count(*) as n from entity_review")[0][0]
    pending = q("select count(*) as n from entity_review "
                "where status = 'pending'")[0][0]
    return {"candidates": len(rows), "before": before, "after": after,
            "inserted": after - before, "pending": pending}


def publish(
    con: duckdb.DuckDBPyConnection,
    plan_year: int,
    out_dir: Path,
    *,
    table: str = "f5500_resolved",
    min_rows: int = 100_000,
) -> Any:
    """Write one plan year's sponsors to Parquet and assert freshness.

    Government data, so this is the GitHub Releases side of the licensing
    boundary. The file is written locally; uploading it to a Release is a
    separate, deliberate step.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"form5500_sponsors_{plan_year}.parquet"
    con.execute(f"""
        copy (
            select plan_year, ein, sponsor_name, dba_name, naics, city, state,
                   zip, plans, participants_sum, participants_max,
                   dfe_code, is_dfe, files_short_form, files_main_form,
                   ein_cik as cik, ein_tickers as tickers, by_ein,
                   by_ein_listed,
                   -- Distinguishes "matches nothing" from "name matched but
                   -- the EIN disagreed". Both lack an EIN match, but only the
                   -- first is confidently private; the second is a question
                   -- in the review queue and must not be counted as either.
                   (exact_cik is not null or norm_cik is not null)
                       as name_matched
            from {table}
        ) to '{dest.as_posix()}' (format parquet)
    """)
    rel = con.read_parquet(dest.as_posix())
    observed = assert_fresh(
        DATASET, rel, partition=str(plan_year), min_rows=min_rows,
        # A plan year has no observation date of its own; the freshness that
        # matters is row count and the partial-year check in completeness().
        date_column=None,
        expect_cols=("plan_year", "ein", "sponsor_name", "naics", "is_dfe",
                     "by_ein", "name_matched"),
    )
    manifest.record_stats(observed, con=con)
    log.info("wrote %s", dest)
    return observed
