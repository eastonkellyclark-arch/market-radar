"""EDGAR's current-filings feed, filtered by form type.

The tripwire for Tier 1. Filings are legally required, timestamped, and
unambiguous, which is why deal detection keys on form type rather than on
news — see the M&A note in CLAUDE.md.

The form types here are the ones that mean something on their own:

    4          insider transaction; the input to cluster detection
    8-K        material event; deal items get extracted later
    S-4        registration for a stock-financed merger
    DEFM14A    definitive merger proxy
    SC 13D     activist stake, >5%, with intent
    SC TO-T    third-party tender offer
    SC 13E-3   going-private transaction

**This feed is a tripwire, not a history.** ``getcurrent`` returns only the
most recent few hundred filings across all of EDGAR, so a poll that runs
hourly sees everything and a poll that runs daily does not. Reading a week
back is a job for the daily index files, not for this.

SEC blocks traffic without a descriptive User-Agent carrying a real contact
address, and asks for no more than 10 requests a second. One request per form
type per poll is seven requests, so the ceiling is never in play; the pacer is
here because the ceiling is a rule rather than a suggestion.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Iterable
from xml.etree import ElementTree as ET

import duckdb
import httpx

from marketradar import manifest, storage
from marketradar.freshness import assert_fresh

log = logging.getLogger(__name__)

ENV_USER_AGENT: Final[str] = "MR_SEC_USER_AGENT"
KIND: Final[str] = "edgar_filing"
SOURCE: Final[str] = "edgar_rss"

#: Form types worth waking up for.
FORM_TYPES: Final[tuple[str, ...]] = (
    "4", "8-K", "S-4", "DEFM14A", "SC 13D", "SC TO-T", "SC 13E-3",
)

#: SEC's published ceiling is 10 requests/second. Eight leaves headroom for
#: anything else of ours that happens to be talking to them.
REQUESTS_PER_SECOND: Final[float] = 8.0
REQUEST_TIMEOUT: Final[float] = 60.0

#: Entries per form type per poll. The feed caps out around 100 anyway.
ENTRIES_PER_FORM: Final[int] = 100

#: A poll that returns nothing at all is a broken poll, not a quiet market:
#: EDGAR takes thousands of filings a day and 4s alone run ~900.
MIN_ROWS: Final[int] = 1

_ATOM: Final[str] = "{http://www.w3.org/2005/Atom}"

#: Accession numbers appear in the entry id and in the filing URL, in the
#: dashed form in one and the bare form in the other.
_ACCESSION = re.compile(r"(\d{10})-?(\d{2})-?(\d{6})")


class EdgarError(RuntimeError):
    """EDGAR could not be reached, or answered with something unusable."""


class Pacer:
    """Spaces requests to stay under SEC's per-second ceiling."""

    def __init__(self, per_second: float = REQUESTS_PER_SECOND) -> None:
        self.interval = 1.0 / per_second
        self._last = 0.0

    def wait(self) -> None:
        gap = self.interval - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()


@dataclass(frozen=True, slots=True)
class Filing:
    accession: str
    form_type: str
    company: str
    cik: str | None
    filed_at: datetime
    url: str
    title: str

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "form_type": self.form_type,
            "company": self.company,
            "cik": self.cik,
            "title": self.title,
        }


def user_agent() -> str:
    """SEC blocks requests without a descriptive UA and a real contact."""
    ua = os.environ.get(ENV_USER_AGENT, "").strip()
    if not ua or "dummy" in ua.lower() or "example.com" in ua.lower():
        raise EdgarError(
            f"{ENV_USER_AGENT} is not set to a real contact address. SEC blocks "
            "traffic without a descriptive User-Agent and it must reach a human."
        )
    if "@" not in ua:
        raise EdgarError(
            f"{ENV_USER_AGENT}={ua!r} has no email address in it. SEC asks for "
            "a contact, not just a product name."
        )
    return ua


def normalize_accession(value: str | None) -> str | None:
    """The dashed form, from whichever shape EDGAR happened to use."""
    if not value:
        return None
    m = _ACCESSION.search(value)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _text(node: Any, tag: str) -> str:
    found = node.find(f"{_ATOM}{tag}")
    return (found.text or "").strip() if found is not None else ""


def _parse_when(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_feed(xml: str, form_type: str) -> list[Filing]:
    """Atom entries to filings. Malformed entries are skipped, not fatal.

    One bad entry must not cost the poll: the feed is the only view of a
    window that has already passed, and there is no second chance at it.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise EdgarError(f"{form_type}: feed was not valid XML: {exc}") from exc

    out: list[Filing] = []
    for entry in root.findall(f"{_ATOM}entry"):
        link = entry.find(f"{_ATOM}link")
        url = (link.get("href") or "") if link is not None else ""
        accession = normalize_accession(_text(entry, "id")) or normalize_accession(url)
        filed_at = _parse_when(_text(entry, "updated"))
        if not accession or filed_at is None:
            log.debug("%s: skipping entry with no accession or date", form_type)
            continue

        title = _text(entry, "title")
        # "4 - COMPANY NAME (0001234567) (Reporting)"
        company = title
        if " - " in title:
            company = title.split(" - ", 1)[1]
        cik = None
        cik_match = re.search(r"\((\d{7,10})\)", company)
        if cik_match:
            cik = cik_match.group(1).zfill(10)
            company = company[: cik_match.start()].strip()

        out.append(
            Filing(
                accession=accession,
                form_type=form_type,
                company=company.strip(" -"),
                cik=cik,
                filed_at=filed_at,
                url=url,
                title=title,
            )
        )
    return out


def fetch(
    form_types: Iterable[str] = FORM_TYPES,
    client: httpx.Client | None = None,
    count: int = ENTRIES_PER_FORM,
) -> list[Filing]:
    """One request per form type. Seven requests, well under the ceiling."""
    base = manifest.get("edgar", "current").location
    headers = {"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"}
    owns = client is None
    client = client or httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True)
    pacer = Pacer()

    seen: set[tuple[str, str]] = set()
    out: list[Filing] = []
    try:
        for form_type in form_types:
            pacer.wait()
            try:
                resp = client.get(
                    base,
                    params={
                        "action": "getcurrent",
                        "type": form_type,
                        "company": "",
                        "dateb": "",
                        "owner": "include",
                        "count": str(count),
                        "output": "atom",
                    },
                    headers=headers,
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 403:
                    raise EdgarError(
                        "EDGAR returned 403. This is almost always the "
                        f"User-Agent: {ENV_USER_AGENT} must be descriptive and "
                        "carry a real contact address."
                    ) from exc
                raise EdgarError(
                    f"{form_type}: EDGAR returned HTTP {exc.response.status_code}"
                ) from exc
            except httpx.HTTPError as exc:
                raise EdgarError(f"{form_type}: could not reach EDGAR: {exc}") from exc

            for filing in parse_feed(resp.text, form_type):
                # EDGAR's type filter is a prefix match: asking for "4" also
                # returns 4/A, and asking for "S-4" also returns S-4/A. Both
                # are wanted, but the same document must not be counted once
                # per form type it happens to prefix-match.
                key = (filing.accession, filing.form_type)
                if key in seen:
                    continue
                seen.add(key)
                out.append(filing)
    finally:
        if owns:
            client.close()
    return out


def load(
    filings: list[Filing], con: duckdb.DuckDBPyConnection | None = None
) -> dict[str, int]:
    """Upsert into ``signals``. Idempotent on (kind, accession).

    Keyed on the accession number, not on the timestamp. 001's natural key
    would reject a second filing from the same company at the same instant --
    which is exactly what an insider cluster looks like. See sql/004.
    """
    con = con or storage.connect()
    if not storage.postgres_attached(con):
        raise EdgarError("No Postgres attached; cannot upsert signals.")

    def ex(sql: str) -> None:
        con.execute("CALL postgres_execute('pg', ?)", [sql])

    def q(sql: str) -> list[tuple]:
        return con.execute("SELECT * FROM postgres_query('pg', ?)", [sql]).fetchall()

    def lit(value: str | None) -> str:
        if value is None:
            return "null"
        return "'" + str(value).replace("'", "''") + "'"

    before = q(f"select count(*) from signals where kind = '{KIND}'")[0][0]

    import json

    for chunk in (filings[i : i + 200] for i in range(0, len(filings), 200)):
        values = ", ".join(
            "({}, {}, {}, timestamptz {}, {}::jsonb, {}, {})".format(
                "null",                                  # company_id: resolved later
                lit(KIND),
                lit(SOURCE),
                lit(f.filed_at.isoformat()),
                lit(json.dumps(f.payload, separators=(",", ":"))),
                lit(f.url),
                lit(f.accession),
            )
            for f in chunk
        )
        ex(
            "insert into signals "
            "(company_id, kind, source, occurred_at, payload, url, accession) "
            f"values {values} "
            "on conflict (kind, accession) where accession is not null "
            "do update set payload = excluded.payload, url = excluded.url"
        )

    after = q(f"select count(*) from signals where kind = '{KIND}'")[0][0]

    stored = con.sql(
        "SELECT * FROM postgres_query('pg', "
        f"'select accession, occurred_at::date as date, payload from signals "
        f"where kind = ''{KIND}''')"
    )
    assert_fresh(
        KIND, stored, partition="all", min_rows=MIN_ROWS,
        date_column="date", expect_cols=("accession", "date"),
    )

    return {
        "fetched": len(filings),
        "before": before,
        "after": after,
        "inserted": after - before,
    }
