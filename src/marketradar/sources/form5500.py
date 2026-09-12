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

#: A plan effective date at or before this is not a date. The column runs
#: 1876-11-11 to 2027-08-01 in the 2024 file: the early end predates ERISA
#: (1974) by a century and the late end is in the future, so both are typing
#: errors rather than facts. The upper bound is not a constant because it
#: depends on the plan year -- see :func:`build_sponsors`.
#:
#: The bound is exclusive, which matters for exactly one value: 1900-01-01 is
#: a placeholder that 10 sponsors carry, and once the screen sorted by age it
#: took the top three slots -- including an *LLC*, a form that did not exist
#: in 1900. Jan 1 dates in general are kept: a plan year almost always starts
#: on January 1, so it is the most common legitimate effective date in the
#: file and filtering the lot would discard 116 real ones to remove 10 fake.
#:
#: A plan cannot predate the company sponsoring it, which is what makes the
#: oldest plan a *floor* on entity age and never the age itself.
EFF_DATE_FLOOR: Final[date] = date(1900, 1, 1)

#: ``TYPE_PLAN_ENTITY_CD`` -- who the plan covers. Not to be confused with
#: the DFE code below, and not numbered in the order anyone would guess: the
#: single-employer case is 2, not 1. Measured on the 2024 main form, where 2
#: is 207,376 of 225,591 filings and 4 matches the DFE count exactly.
PLAN_ENTITY: Final[dict[str, str]] = {
    "1": "multiemployer",
    "2": "single-employer",
    "3": "multiple-employer",
    "4": "direct filing entity",
}

#: A multiemployer plan is jointly administered by a board of trustees under
#: a collective bargaining agreement, and its sponsor is that board -- a
#: trust covering workers across an entire trade in a region, not a company.
#: Its participant count is an industry's headcount and moves with the trade.
#:
#: Same trap as the DFE flag and it took the same list: five of the first
#: twelve mature-target candidates were boards of trustees for tile layers,
#: masons and carpenters, because a trade in decline looks exactly like an
#: employer in decline and is very old and very large.
#:
#: **Main form only.** ``SF_PLAN_ENTITY_CD`` looks like the same column and
#: is not: on the short form 1 means *single-employer* and covers 795,824 of
#: 798,006 filings. Applying this code to both forms marked the whole private
#: population as multiemployer and silently removed 800,287 sponsors from the
#: screen -- which read as a working filter, because the survivors were
#: plausible and the count was merely smaller. Two files, two vocabularies,
#: one column name.
MULTIEMPLOYER_CODE: Final[str] = "1"

#: Pension characteristic codes for a 403(b) arrangement: ``2L`` is a
#: 403(b)(1) annuity and ``2M`` a 403(b)(7) custodial account. **Only a
#: 501(c)(3) or a public school may sponsor one**, so this is a structural
#: fact about the sponsor rather than an inference about it -- the same
#: quality of evidence as an EIN, and the reason the flag has two tiers.
NONPROFIT_PLAN_CODES: Final[tuple[str, ...]] = ("2L", "2M")

#: NAICS codes where nonprofits dominate, chosen by measurement rather than
#: intuition: these are the codes with more than 500 sponsors where at least
#: 10% carry a 403(b), which is a floor on how nonprofit-dense the code is,
#: since a nonprofit may equally sponsor a 401(k). Measured 2026-09-10 on
#: plan year 2024.
#:
#: **This tier is a guess and is labelled as one.** 622000 (hospitals),
#: 623000 (nursing homes) and 611000 (educational services) all contain real
#: for-profit businesses -- a private hospital group, a family nursing home,
#: a trade school -- and this will set those aside too. That is why the basis
#: is carried on the row and the screen can be asked for them: a flag whose
#: evidence is invisible is the fuzzy-name-matching mistake again.
NONPROFIT_NAICS: Final[dict[str, str]] = {
    "712100": "museums & historical sites",
    "622000": "hospitals",
    "611000": "educational services",
    "624100": "individual & family services",
    "813000": "religious, grantmaking & civic",
    "624200": "community food & housing",
    "624310": "vocational rehabilitation",
    "711100": "performing arts",
    "621420": "outpatient mental health",
    "623000": "nursing & residential care",
    "515100": "broadcasting",
    "621498": "outpatient care centres",
}

#: What ``nonprofit_basis`` can say. ``plan_type`` is the structural one and
#: the only one that is certain; ``naics`` is a sector guess.
NONPROFIT_PLAN: Final[str] = "plan_type"
NONPROFIT_NAICS_ONLY: Final[str] = "naics"
NONPROFIT_BOTH: Final[str] = "both"

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
    "active": "TOT_ACT_PARTCP_BOY_CNT",
    "dfe": "TYPE_DFE_PLAN_ENTITY_CD",
    "entity": "TYPE_PLAN_ENTITY_CD",
    "pension_code": "TYPE_PENSION_BNFT_CODE",
    "eff_date": "PLAN_EFF_DATE",
    "plan_num": "SPONS_DFE_PN",
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
    "active": "SF_TOT_ACT_PARTCP_BOY_CNT",
    "dfe": None,
    # Not SF_PLAN_ENTITY_CD. The short form uses a *different vocabulary* for
    # the same-looking column: there, 1 is single-employer (795,824 of
    # 798,006 filings for 2024), while on the main form 1 is multiemployer.
    # Reading them with one mapping flagged the entire private population as
    # union trusts and cut the candidate pool by 800,000 -- see the note on
    # MULTIEMPLOYER_CODE. A multiemployer plan is collectively bargained and
    # large, so it files the main form; the short form needs no such flag.
    "entity": None,
    "pension_code": "SF_TYPE_PENSION_BNFT_CODE",          # the short form has no DFE concept
    "eff_date": "SF_PLAN_EFF_DATE",
    "plan_num": "SF_PLAN_NUM",
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


def _csv_columns(con: duckdb.DuckDBPyConnection, csv_path: Path) -> list[str]:
    """The header of one DOL CSV, as DuckDB sees it."""
    rows = con.execute(f"""
        describe select * from read_csv_auto('{csv_path.as_posix()}',
                                             all_varchar=true,
                                             ignore_errors=true)
    """).fetchall()
    return [r[0] for r in rows]


def _check_columns(
    present: Iterable[str], cols: dict[str, str | None], csv_path: Path,
    year: int, form: str,
) -> None:
    """Fail on a renamed or dropped DOL column, by name, before any SQL runs.

    DOL changes the schema between plan years, and without this the failure
    is a DuckDB ``BinderException`` naming one column and pointing at a
    generated SELECT -- which says nothing about which year's file broke or
    what the loader expected. Every column in the map is required: a
    silently-null field would flow into the sponsor table and read as
    missing data rather than as a schema change.
    """
    have = {c.upper() for c in present}
    missing = sorted(
        {name for name in cols.values() if name} - have
    )
    if not missing:
        return

    # A renamed column usually keeps its distinctive word, so offer the
    # file's own near-matches rather than making the reader diff a header of
    # several hundred fields by hand.
    hints = []
    for name in missing:
        token = max(name.split("_"), key=len)
        near = sorted(c for c in have if token in c)[:3]
        hints.append(f"    {name}" + (f"  (file has: {', '.join(near)})"
                                      if near else "  (no near match)"))
    raise Form5500Error(
        f"{csv_path.name} (plan year {year}, {form} form) is missing "
        f"{len(missing)} mapped column(s):\n" + "\n".join(hints) +
        f"\n  The file has {len(have)} columns. DOL changes the schema "
        f"between plan years; update _MAIN_COLS/_SF_COLS in "
        f"{__name__} rather than letting the field load as null."
    )


def _select(csv_path: Path, cols: dict[str, str | None], year: int,
            form: str) -> str:
    """One SELECT that normalises a form's columns into the common shape."""
    def col(key: str) -> str:
        name = cols.get(key)
        return f'trim("{name}")' if name else "cast(null as varchar)"

    dfe = cols.get("dfe")
    dfe_expr = f'upper(trim("{dfe}"))' if dfe else "cast(null as varchar)"
    entity = cols.get("entity")
    entity_expr = f'upper(trim("{entity}"))' if entity else "cast(null as varchar)"
    pension = cols.get("pension_code")
    pension_expr = (f'upper(coalesce("{pension}", \'\'))' if pension
                    else "cast('' as varchar)")
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
            -- Active participants: employees still accruing. The total above
            -- also counts retirees and separated ex-employees who still hold
            -- a balance, which is 22% of the main form's participants and is
            -- not headcount. Never coalesced to zero -- a plan that does not
            -- report it has an unknown active count, not an empty one.
            try_cast({col('active')} as bigint)       as active,
            try_cast({col('eff_date')} as date)       as plan_eff_date,
            -- (EIN, plan number) is what DOL uses to identify a plan across
            -- years. Without it the only comparable unit is the sponsor, and
            -- a sponsor's *set* of filed plans changes year to year.
            nullif(trim({col('plan_num')}), '')       as plan_num,
            nullif({dfe_expr}, '')       as dfe_code,
            nullif({entity_expr}, '')    as entity_code,
            {pension_expr}               as pension_code
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
        _check_columns(_csv_columns(con, csv_path), cols, csv_path,
                       archive.year, archive.kind)
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


#: ``2L``/``2M`` appear inside a concatenated code string like ``2E2F2G2L``,
#: so this is a substring test rather than an equality one.
_HAS_403B: Final[str] = " or ".join(
    f"pension_code like '%{code}%'" for code in NONPROFIT_PLAN_CODES)

_NP_NAICS_SQL: Final[str] = ", ".join(f"'{c}'" for c in NONPROFIT_NAICS)


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

    **The per-plan counts are carried too, keyed on the plan number.** A
    sponsor's *set* of filed plans is not stable between years -- a plan is
    added, terminated, or simply filed late -- so any aggregate over "the
    plans filed this year" is comparing different things at each end. Edward
    Don & Company filed two plans for 2022 and one for 2024, which read as a
    46% headcount collapse and is a plan that is not in the file. The list
    here is what lets :func:`build_trend` compare the plans a sponsor filed
    in *both* years and leave the rest out of the arithmetic. See the
    identifier rule in CLAUDE.md: ``(ein, plan_num)`` is DOL's own key for a
    plan, and it is the one the comparison has to run on.
    """
    con.execute(f"drop table if exists {table}")
    con.execute(f"""
        create table {table} as
        with per_plan as (
            -- One row per plan, not per filing. An amended filing repeats
            -- the plan, and summing over filings counts those people twice.
            -- Plans with no number fall back to the plan name, and then to a
            -- fixed key: unusable for matching across years, but still one
            -- row rather than several.
            select plan_year, ein,
                   coalesce(plan_num, plan_name, '?')     as plan_key,
                   max(coalesce(participants, 0))         as participants,
                   max(active)                            as active,
                   min(sponsor_name)                      as sponsor_name,
                   max(dba_name)                          as dba_name,
                   max(naics)                             as naics,
                   max(city)                              as city,
                   max(state)                             as state,
                   max(zip)                               as zip,
                   max(dfe_code)                          as dfe_code,
                   max(entity_code)                       as entity_code,
                   bool_or({_HAS_403B})                   as has_403b,
                   bool_or(form = 'short')                as short_form,
                   bool_or(form = 'main')                 as main_form,
                   min(case when plan_eff_date > DATE '{EFF_DATE_FLOOR}'
                             and plan_eff_date
                                 <= make_date(plan_year + 1, 1, 1)
                            then plan_eff_date end)       as plan_eff
            from {filings}
            where ein is not null
            group by plan_year, ein, plan_key
        )
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
            -- ::bigint because sum() over a bigint yields HUGEINT, which
            -- parquet has no type for and stores as DOUBLE -- so a
            -- headcount came back from the published file as 140.0.
            sum(participants)::bigint                     as participants_sum,
            max(participants)                             as participants_max,
            sum(active)::bigint                           as active_sum,
            max(active)                                   as active_max,
            -- The per-plan detail, so a later year can be compared against
            -- this one plan by plan rather than total against total.
            list(struct_pack(plan := plan_key,
                             participants := participants,
                             active := active)
                 order by plan_key)                       as plan_counts,
            -- The oldest plan the sponsor still files: a floor on entity
            -- age, not the age. A 1985 plan means the company existed in
            -- 1985 and says nothing about how long before that. min() so it
            -- is deterministic across runs.
            --
            -- Both ends are bounded because both ends hold typos: the column
            -- runs 1876 to 2027 in the 2024 file. The ceiling is the January
            -- after the plan year, since a plan cannot take effect after the
            -- year it is being reported for.
            min(plan_eff)                                 as oldest_plan_eff,
            -- Flagged, never dropped: a DFE is a trustee, and mixing one into
            -- an employer list is how 'private companies' comes back as
            -- Transamerica Life and BNY Mellon.
            max(dfe_code)                                 as dfe_code,
            bool_or(dfe_code is not null)                 as is_dfe,
            -- Flagged, not dropped, exactly like the DFE beside it: a board
            -- of trustees is a real filer and a real participant count, and
            -- it is not an employer with a headcount.
            max(entity_code)                              as entity_code,
            -- coalesce, because the short form contributes no entity code
            -- and bool_or over nothing but nulls is null, not false. A null
            -- here fails `not is_multiemployer` in the screen, which drops
            -- the row -- the same 800,000 disappearing again by a different
            -- route, and just as quietly.
            coalesce(bool_or(entity_code = '{MULTIEMPLOYER_CODE}'), false)
                                                          as is_multiemployer,
            -- Two tiers, and which one is on the row is the point. A 403(b)
            -- can only be sponsored by a 501(c)(3) or a public school, so it
            -- is a fact; the NAICS set is a sector guess that will also catch
            -- for-profit hospitals and trade schools. Flagged, never dropped,
            -- and the screen reports each tier separately.
            case when coalesce(bool_or(has_403b), false)
                      and max(naics) in ({_NP_NAICS_SQL})
                     then '{NONPROFIT_BOTH}'
                 when coalesce(bool_or(has_403b), false)
                     then '{NONPROFIT_PLAN}'
                 when max(naics) in ({_NP_NAICS_SQL})
                     then '{NONPROFIT_NAICS_ONLY}'
                 end                                      as nonprofit_basis,
            bool_or(short_form)                           as files_short_form,
            bool_or(main_form)                            as files_main_form
        from per_plan
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

    if expected == 0:
        # A plan year with no sponsors is the failure this project cares most
        # about wearing its friendliest face: every downstream count is zero,
        # every ratio is a division by zero, and nothing errors. Before this
        # guard it surfaced as a TypeError on a null aggregate.
        raise Form5500Error(
            f"{sponsors} is empty. A plan year with no sponsors means the "
            "download or the parse failed, not that nobody filed."
        )

    row = con.execute(f"""
        select min(plan_year), count(*),
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


# --- the participant time series -----------------------------------------


#: What ``status`` can say about a sponsor. Kept apart from ``trend`` on
#: purpose: the failure this vocabulary exists to prevent is "stopped filing"
#: being read as "shrank to nothing".
STATUS_FILING: Final[str] = "filing"
STATUS_LAPSED: Final[str] = "lapsed"

#: What ``trend`` can say about the participant series. ``unknown`` is a real
#: answer and by far the most common one -- a sponsor with a single filed year
#: has no trend, and inventing one out of an absence is the whole bug.
TREND_GROWING: Final[str] = "growing"
TREND_FLAT: Final[str] = "flat"
TREND_DECLINING: Final[str] = "declining"
TREND_UNKNOWN: Final[str] = "unknown"

#: Fractional change across the span inside which a sponsor is called flat
#: rather than growing or declining. Participants are a beginning-of-year
#: headcount the sponsor reports by hand, so a few percent is reporting noise.
FLAT_BAND: Final[float] = 0.05

#: Columns a published sponsor parquet must carry to join into the series.
_HISTORY_COLS: Final[tuple[str, ...]] = (
    "plan_year", "ein", "sponsor_name", "naics", "city", "state", "zip",
    "plans", "participants_sum", "participants_max",
    "active_sum", "active_max", "entity_code", "is_multiemployer",
    "nonprofit_basis",
    "plan_counts",
    "oldest_plan_eff", "is_dfe", "by_ein", "by_ein_listed", "name_matched",
    "files_main_form", "files_short_form",
)


def complete_years(
    years: Iterable[int], *, newest_complete: int = NEWEST_COMPLETE_YEAR
) -> list[int]:
    """Which of the loaded plan years are finished being filed.

    A year past :data:`NEWEST_COMPLETE_YEAR` is still arriving, and anything
    concluded from it is a filing lag rather than a fact about employers. The
    trend is measured across the complete ones only.
    """
    return sorted({int(y) for y in years if int(y) <= newest_complete})


def _parquet_columns(con: duckdb.DuckDBPyConnection, path: Path) -> list[str]:
    """Column names in a parquet file, without reading its rows."""
    rows = con.execute(
        f"describe select * from read_parquet('{path.as_posix()}')"
    ).fetchall()
    return [r[0] for r in rows]


def build_history(
    con: duckdb.DuckDBPyConnection,
    sources: dict[int, Path],
    *,
    table: str = "f5500_history",
) -> int:
    """Stack published sponsor parquets into one (plan year, EIN) series.

    Reads the *published* per-year files rather than re-parsing the DOL
    archives: the EIN resolution and the DFE flag already live there, and a
    series that disagreed with the private-company panel about who is private
    would be worse than no series at all.
    """
    if not sources:
        raise Form5500Error("no plan years given")

    parts = []
    for year in sorted(sources):
        path = sources[year]
        if not path.exists():
            raise Form5500Error(f"plan year {year}: {path} does not exist")
        have = {c.upper() for c in _parquet_columns(con, path)}
        missing = sorted({c.upper() for c in _HISTORY_COLS} - have)
        if missing:
            raise Form5500Error(
                f"{path.name} is missing {', '.join(missing).lower()}. It was "
                "written by an older loader; re-run `mr form5500 --year "
                f"{year}` to rewrite it rather than joining a partial shape."
            )
        parts.append(
            f"select {', '.join(_HISTORY_COLS)} "
            f"from read_parquet('{path.as_posix()}')"
        )

    con.execute(f"drop table if exists {table}")
    con.execute(f"create table {table} as "
                + " union all by name ".join(parts))
    dupes = con.execute(f"""
        select count(*) from (
            select ein, plan_year from {table}
            group by 1, 2 having count(*) > 1
        )
    """).fetchone()[0]
    if dupes:
        raise Form5500Error(
            f"{dupes:,} (EIN, plan year) pairs appear twice in {table}. The "
            "series is keyed on that pair, so a duplicate means two files "
            "carry the same plan year and every delta below is doubled."
        )
    return int(con.execute(f"select count(*) from {table}").fetchone()[0])


@dataclass(frozen=True, slots=True)
class TrendBuild:
    """What building the series covered, and what it could not."""

    sponsors: int
    complete_years: tuple[int, ...]
    partial_years: tuple[int, ...]
    #: Sponsors seen *only* in a year still being filed. They have no
    #: complete year, so they have no series and no row in the trend table.
    #: Reported rather than dropped quietly: without this number, a caller
    #: comparing the history to the trend finds a discrepancy and no reason
    #: for it.
    only_partial: int

    def __str__(self) -> str:
        done = ", ".join(str(y) for y in self.complete_years)
        tail = (f"; {self.only_partial:,} seen only in "
                f"{', '.join(str(y) for y in self.partial_years)} and so "
                "have no series yet") if self.only_partial else ""
        return f"{self.sponsors:,} sponsors over plan years {done}{tail}"


def build_trend(
    con: duckdb.DuckDBPyConnection,
    *,
    history: str = "f5500_history",
    table: str = "f5500_trend",
    newest_complete: int | None = None,
) -> TrendBuild:
    """One row per EIN: the participant series, and what its absences mean.

    **Presence and trend are separate columns, and that is the entire point.**
    A sponsor missing from a later year has not necessarily shrunk. It may
    have terminated the plan, been acquired, changed EIN, dropped below the
    filing threshold, or -- overwhelmingly in the newest year -- simply not
    filed yet, because filings lag the plan year by about eighteen months.
    Folding an absence into the participant series as a zero manufactures a
    cliff for a large share of the file, and a screen looking for declining
    headcount would sort exactly those to the top.

    So the answer is columns that cannot be mistaken for each other:

    ``status``
        ``filing`` if the sponsor appears in the newest complete plan year or
        any later one; ``lapsed`` if its newest filing is older than that. A
        lapse is a question -- terminated, acquired, re-EIN'd, shrunk below
        the threshold -- and never on its own a headcount decline.
    ``trend``
        Measured **only between plan years the sponsor actually filed, and
        only complete ones**, or ``unknown`` when there are fewer than two of
        those. An absent year contributes no data point in either direction.
    ``pending_years`` / ``gap_years``
        Absences, split by what they can mean. ``pending`` is a year still
        being filed and carries no information at all; ``gap`` is a complete
        year skipped between two the sponsor filed, which is a real oddity.

    **The same problem exists one level down, at the plan.** A sponsor's set
    of filed plans is not stable between years -- plans are opened, merged,
    terminated, or filed late -- so comparing "everything it filed in 2022"
    against "everything it filed in 2024" compares two different things and
    calls the difference a headcount change. Measured against the real file:
    Edward Don & Company filed two plans for 2022 and one for 2024 and read
    as -46%; The Juilliard School's largest single plan went 1,473 -> 500 ->
    981 while its total barely moved, because *which* of its six plans was
    largest kept changing.

    So the trend is computed over the plans present in **both** endpoint
    years, keyed on ``(ein, plan_num)`` -- DOL's own identifier for a plan --
    and ``plans_added``/``plans_dropped`` report the rest as counts rather
    than folding them into the arithmetic. ``matched_first`` and
    ``matched_last`` are the two comparable totals; ``participants_last`` and
    ``participants_sum`` stay as they were, the headcount bounds for display.
    A sponsor with no plan in common between its endpoints has no comparable
    pair and its trend is ``unknown``.
    """
    years = [int(r[0]) for r in con.execute(
        f"select distinct plan_year from {history} order by 1").fetchall()]
    if not years:
        raise Form5500Error(f"{history} is empty; nothing to trend")
    newest = NEWEST_COMPLETE_YEAR if newest_complete is None else newest_complete
    done = complete_years(years, newest_complete=newest)
    if not done:
        raise Form5500Error(
            f"none of the loaded plan years {years} is complete (newest "
            f"complete is {newest}). Every trend would be measuring a filing "
            "lag rather than an employer."
        )
    # Written once and interpolated: it appears three times below, and a
    # copy that drifted would file a sponsor under a trend its own printed
    # percentage contradicts.
    _PCT = ("(list_sum(list_transform(m.common_last, l -> l.active))"
            " - list_sum(list_transform(m.common_first, f -> f.active)))"
            " / list_sum(list_transform(m.common_first,"
            " f -> f.active))::double")
    partial = [y for y in years if y > newest]
    done_sql = "[" + ", ".join(str(y) for y in done) + "]"
    # An empty list literal has no type DuckDB can infer, and with no partial
    # year loaded the count is known to be zero anyway.
    pending_sql = (
        f"len(list_filter([{', '.join(str(y) for y in partial)}], "
        "y -> y > p.last_seen_year))" if partial else "0"
    )

    con.execute(f"drop table if exists {table}")
    con.execute(f"""
        create table {table} as
        with complete as (
            select * from {history}
            where plan_year in ({', '.join(str(y) for y in done)})
        ),
        agg as (
            select
                ein,
                -- min(), like the sponsor name in build_sponsors and for the
                -- same reason: one EIN carries several spellings across years,
                -- and an arbitrary pick makes the output depend on the run.
                min(sponsor_name)                    as sponsor_name,
                -- Everything else describes the sponsor *now*, so it comes
                -- from the newest year it filed. Deterministic because
                -- (ein, plan_year) is unique -- build_history proves it.
                max_by(naics, plan_year)             as naics,
                max_by(city, plan_year)              as city,
                max_by(state, plan_year)             as state,
                max_by(zip, plan_year)               as zip,
                max_by(plans, plan_year)             as plans,
                max_by(by_ein, plan_year)            as by_ein,
                max_by(by_ein_listed, plan_year)     as by_ein_listed,
                max_by(name_matched, plan_year)      as name_matched,
                max_by(participants_sum, plan_year)  as participants_sum,
                max_by(participants_max, plan_year)  as participants_last,
                max_by(active_sum, plan_year)        as active_sum,
                max_by(active_max, plan_year)        as active_last,
                max_by(plan_counts, plan_year)       as last_plans,
                min_by(plan_counts, plan_year)       as first_plans,
                min(oldest_plan_eff)                 as oldest_plan_eff,
                bool_or(is_dfe)                      as is_dfe,
                bool_or(is_multiemployer)            as is_multiemployer,
                max_by(nonprofit_basis, plan_year)   as nonprofit_basis,
                max_by(entity_code, plan_year)       as entity_code,
                min(plan_year)                       as first_year,
                max(plan_year)                       as last_year,
                count(*)                             as years_filed,
                list(struct_pack(year := plan_year,
                                 participants := active_sum)
                     order by plan_year)             as series
            from complete
            group by ein
        ),
        matched as (
            -- The plans filed in *both* endpoint years, and the headcount
            -- each end reports for exactly those. This is the whole fix: a
            -- sponsor that filed two plans in the first year and one in the
            -- last is not a sponsor that halved, and comparing its totals
            -- says it is.
            -- Plans whose active count is missing at either end are left
            -- out too: an unknown count cannot be compared, and coalescing
            -- it to zero would read as the whole plan being laid off.
            select a.ein,
                   list_filter(a.first_plans,
                       f -> f.active is not null and list_contains(
                           list_transform(list_filter(a.last_plans,
                                                      x -> x.active is not null),
                                          l -> l.plan), f.plan)
                   )                                 as common_first,
                   list_filter(a.last_plans,
                       l -> l.active is not null and list_contains(
                           list_transform(list_filter(a.first_plans,
                                                      x -> x.active is not null),
                                          f -> f.plan), l.plan)
                   )                                 as common_last
            from agg a
        ),
        presence as (
            -- Every loaded year, partial ones included, so "missing from the
            -- year still being filed" stays distinguishable from "missing
            -- from a year that finished three years ago".
            select ein, max(plan_year) as last_seen_year
            from {history} group by ein
        )
        select
            a.* exclude (first_plans, last_plans),
            p.last_seen_year,
            len(m.common_first)                      as common_plans,
            -- Plans in the last year that the first year did not carry, and
            -- the reverse. Reported rather than folded into the trend: a
            -- plan appearing or vanishing is a fact about the filing, and
            -- the trend is a statement about headcount.
            len(a.last_plans) - len(m.common_last)   as plans_added,
            len(a.first_plans) - len(m.common_first) as plans_dropped,
            coalesce(list_sum(list_transform(m.common_first,
                                             f -> f.active)), 0)
                                                     as matched_first,
            coalesce(list_sum(list_transform(m.common_last,
                                             l -> l.active)), 0)
                                                     as matched_last,
            -- Complete years the sponsor skipped *between* two it filed.
            -- Counted against the loaded years rather than the span, so
            -- loading 2022 and 2024 without 2023 does not invent a gap for
            -- every sponsor in the file.
            len(list_filter({done_sql},
                            y -> y >= a.first_year and y <= a.last_year))
                - a.years_filed                      as gap_years,
            {pending_sql}                            as pending_years,
            case when p.last_seen_year >= {newest}
                 then '{STATUS_FILING}' else '{STATUS_LAPSED}' end as status,
            case when a.years_filed < 2 or len(m.common_first) = 0
                      or list_sum(list_transform(m.common_first,
                                                 f -> f.active)) = 0
                     then '{TREND_UNKNOWN}'
                 when {_PCT} > {FLAT_BAND}  then '{TREND_GROWING}'
                 when {_PCT} < -{FLAT_BAND} then '{TREND_DECLINING}'
                 else '{TREND_FLAT}' end             as trend,
            case when a.years_filed < 2 or len(m.common_first) = 0
                      or list_sum(list_transform(m.common_first,
                                                 f -> f.active)) = 0
                 then null else {_PCT} end           as pct_change,
            a.last_year - a.first_year               as span_years
        from agg a
        join presence p using (ein)
        join matched m using (ein)
    """)
    n = int(con.execute(f"select count(*) from {table}").fetchone()[0])
    only_partial = int(con.execute(f"""
        select count(*) from (select distinct ein from {history})
        where ein not in (select ein from {table})
    """).fetchone()[0])
    built = TrendBuild(sponsors=n, complete_years=tuple(done),
                       partial_years=tuple(partial), only_partial=only_partial)
    log.info("%s: %s", table, built)
    return built


@dataclass(frozen=True, slots=True)
class YearShape:
    """What one plan year contributes to the series."""

    plan_year: int
    sponsors: int
    complete: bool

    @property
    def note(self) -> str:
        return "complete" if self.complete else "still being filed"


def series_shape(
    con: duckdb.DuckDBPyConnection, *, history: str = "f5500_history",
    newest_complete: int | None = None,
) -> list[YearShape]:
    """Per-year sponsor counts, each marked complete or still being filed.

    A partial year is genuinely smaller and the difference genuinely means
    nothing. This is what has to sit beside the count so the drop is not read
    as an economic event.
    """
    newest = NEWEST_COMPLETE_YEAR if newest_complete is None else newest_complete
    rows = con.execute(f"""
        select plan_year, count(*) from {history} group by 1 order by 1
    """).fetchall()
    return [YearShape(int(y), int(n), int(y) <= newest) for y, n in rows]


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


def published_sql(table: str = "f5500_resolved") -> str:
    """The published sponsor shape, as one SELECT.

    A function rather than an inline string because :func:`build_history`
    requires this exact set of columns to exist in every year's file. Two
    copies of the list would let the writer and the reader drift apart, and
    the symptom would be a year silently missing from the series.
    """
    return f"""
        select plan_year, ein, sponsor_name, dba_name, naics, city, state,
               zip, plans, participants_sum, participants_max,
               -- Employees still accruing, as against the totals above,
               -- which include everyone with a balance. The trend runs on
               -- these; the totals stay for display as the outer bound.
               active_sum, active_max, entity_code, is_multiemployer,
               nonprofit_basis,
               -- Per-plan, keyed on DOL's plan number. The trend compares
               -- the plans present in both years and nothing else: a
               -- sponsor's set of filed plans changes year to year, and a
               -- total-against-total comparison silently reads a plan that
               -- was not filed as staff who are no longer employed.
               plan_counts,
               -- A floor on entity age, not the age: the sponsor existed at
               -- least this long ago. Carried into the parquet because the
               -- mature-target screen ranks on it and recomputing it would
               -- mean re-reading a million filings per year.
               oldest_plan_eff,
               dfe_code, is_dfe, files_short_form, files_main_form,
               ein_cik as cik, ein_tickers as tickers, by_ein,
               by_ein_listed,
               -- Distinguishes "matches nothing" from "name matched but the
               -- EIN disagreed". Both lack an EIN match, but only the first
               -- is confidently private; the second is a question in the
               -- review queue and must not be counted as either.
               (exact_cik is not null or norm_cik is not null) as name_matched
        from {table}
    """


def publish(
    con: duckdb.DuckDBPyConnection,
    plan_year: int,
    out_dir: Path,
    *,
    table: str = "f5500_resolved",
    min_rows: int = 100_000,
    upload: bool = False,
) -> Any:
    """Write one plan year's sponsors to Parquet and assert freshness.

    Government data, so this is the GitHub Releases side of the licensing
    boundary.

    ``upload`` moves the Release upload inside this function and makes the
    freshness assertion verify the declared location. It used to be a ``gh``
    command typed by hand, outside the codebase and therefore outside every
    check -- which is why 2022, 2023 and 2024 sat declared and unpublished for
    weeks while every local run passed. Off by default so a local build stays
    local; the CLI turns it on.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"form5500_sponsors_{plan_year}.parquet"
    con.execute(f"""
        copy ({published_sql(table)}) to '{dest.as_posix()}' (format parquet)
    """)
    rel = con.read_parquet(dest.as_posix())
    ref = manifest.get(DATASET, str(plan_year))
    if upload:
        from marketradar import storage

        storage.publish_release_asset(
            ref, dest,
            notes="DOL Form 5500 sponsor records, keyed on EIN. Public domain "
                  "and ours to republish.")
    observed = assert_fresh(
        DATASET, rel, partition=str(plan_year), min_rows=min_rows,
        # Verified only when this call published it. Asserting the location on a
        # local-only build would fail every developer machine by construction.
        published=ref if upload else None,
        # A plan year has no observation date of its own; the freshness that
        # matters is row count and the partial-year check in completeness().
        date_column=None,
        expect_cols=("plan_year", "ein", "sponsor_name", "naics", "is_dfe",
                     "by_ein", "name_matched", "oldest_plan_eff"),
    )
    manifest.record_stats(observed, con=con)
    log.info("wrote %s", dest)
    return observed
