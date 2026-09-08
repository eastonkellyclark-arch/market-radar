"""EDGAR's current-filings feed. No network.

The feed is a tripwire over a window that has already passed, so a single
malformed entry must not cost the poll — there is no second chance at it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from marketradar.signals import edgar_rss

ATOM = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Latest Filings</title>
  <entry>
    <title>4 - Sawicki Lisa P (0001628280) (Reporting)</title>
    <link rel="alternate" href="https://www.sec.gov/Archives/edgar/data/320193/000162828026060895/0001628280-26-060895-index.htm"/>
    <updated>2026-09-08T20:02:00-04:00</updated>
    <id>urn:tag:sec.gov,2008:accession-number=0001628280-26-060895</id>
  </entry>
  <entry>
    <title>4 - ROYAL BANK OF CANADA (0000950103) (Reporting)</title>
    <link rel="alternate" href="https://www.sec.gov/Archives/edgar/data/1000/000095010326013663/0000950103-26-013663-index.htm"/>
    <updated>2026-09-08T20:01:00-04:00</updated>
    <id>urn:tag:sec.gov,2008:accession-number=0000950103-26-013663</id>
  </entry>
</feed>"""


# --- credentials --------------------------------------------------------


def test_a_missing_user_agent_is_named(monkeypatch) -> None:
    monkeypatch.delenv(edgar_rss.ENV_USER_AGENT, raising=False)
    with pytest.raises(edgar_rss.EdgarError, match=edgar_rss.ENV_USER_AGENT):
        edgar_rss.user_agent()


def test_a_user_agent_without_a_contact_is_refused(monkeypatch) -> None:
    """SEC asks for a contact, not a product name, and 403s without one."""
    monkeypatch.setenv(edgar_rss.ENV_USER_AGENT, "MarketRadar/1.0")
    with pytest.raises(edgar_rss.EdgarError, match="no email address"):
        edgar_rss.user_agent()


def test_a_placeholder_user_agent_is_refused(monkeypatch) -> None:
    monkeypatch.setenv(edgar_rss.ENV_USER_AGENT, "dummy dummy@example.com")
    with pytest.raises(edgar_rss.EdgarError):
        edgar_rss.user_agent()


# --- accession numbers --------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0001628280-26-060895", "0001628280-26-060895"),
        ("000162828026060895", "0001628280-26-060895"),
        ("urn:tag:sec.gov,2008:accession-number=0001628280-26-060895",
         "0001628280-26-060895"),
        (".../000162828026060895/0001628280-26-060895-index.htm",
         "0001628280-26-060895"),
        ("no digits here", None),
        (None, None),
    ],
)
def test_accession_normalisation(raw, expected) -> None:
    """EDGAR uses the dashed form in the id and the bare form in the path."""
    assert edgar_rss.normalize_accession(raw) == expected


# --- feed parsing -------------------------------------------------------


def test_entries_become_filings() -> None:
    got = edgar_rss.parse_feed(ATOM, "4")
    assert [f.accession for f in got] == [
        "0001628280-26-060895", "0000950103-26-013663",
    ]
    assert got[0].company == "Sawicki Lisa P"
    assert got[0].cik == "0001628280"
    assert got[0].form_type == "4"
    assert got[0].url.endswith("-index.htm")


def test_timestamps_are_normalised_to_utc() -> None:
    got = edgar_rss.parse_feed(ATOM, "4")
    assert got[0].filed_at == datetime(2026, 9, 9, 0, 2, tzinfo=timezone.utc)
    assert got[0].filed_at.tzinfo is timezone.utc


def test_an_entry_with_no_accession_is_skipped_not_fatal() -> None:
    """One bad entry must not cost a poll over a window already gone."""
    broken = ATOM.replace(
        "<id>urn:tag:sec.gov,2008:accession-number=0001628280-26-060895</id>",
        "<id>urn:tag:sec.gov,2008:nothing-useful</id>",
    ).replace(
        'href="https://www.sec.gov/Archives/edgar/data/320193/000162828026060895/0001628280-26-060895-index.htm"',
        'href="https://www.sec.gov/none"',
    )
    got = edgar_rss.parse_feed(broken, "4")
    assert len(got) == 1
    assert got[0].accession == "0000950103-26-013663"


def test_an_entry_with_an_unparseable_date_is_skipped() -> None:
    broken = ATOM.replace("2026-09-08T20:02:00-04:00", "not a date")
    assert len(edgar_rss.parse_feed(broken, "4")) == 1


def test_a_feed_that_is_not_xml_raises() -> None:
    with pytest.raises(edgar_rss.EdgarError, match="not valid XML"):
        edgar_rss.parse_feed("<html>rate limited</html><", "4")


def test_an_empty_feed_parses_to_nothing() -> None:
    empty = '<feed xmlns="http://www.w3.org/2005/Atom"><title>x</title></feed>'
    assert edgar_rss.parse_feed(empty, "SC 13D") == []


def test_payload_carries_the_form_type_and_company() -> None:
    f = edgar_rss.parse_feed(ATOM, "4")[0]
    assert f.payload["form_type"] == "4"
    assert f.payload["cik"] == "0001628280"


# --- fetching -----------------------------------------------------------


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_the_same_filing_is_not_counted_once_per_form_type(monkeypatch) -> None:
    """EDGAR's type filter is a prefix match: "4" also returns 4/A, and "S-4"
    also returns S-4/A. Both are wanted; the document is still one document."""
    monkeypatch.setenv(edgar_rss.ENV_USER_AGENT, "Market Radar you@example.org")
    monkeypatch.setattr(edgar_rss, "user_agent", lambda: "x you@example.org")

    got = edgar_rss.fetch(
        form_types=("4", "4"),
        client=_client(lambda r: httpx.Response(200, text=ATOM)),
    )
    assert len(got) == 2, "same form type twice must not double the filings"


def test_a_403_names_the_user_agent(monkeypatch) -> None:
    monkeypatch.setattr(edgar_rss, "user_agent", lambda: "x you@example.org")
    with pytest.raises(edgar_rss.EdgarError, match=edgar_rss.ENV_USER_AGENT):
        edgar_rss.fetch(
            form_types=("4",),
            client=_client(lambda r: httpx.Response(403, text="denied")),
        )


def test_every_watched_form_type_is_requested(monkeypatch) -> None:
    monkeypatch.setattr(edgar_rss, "user_agent", lambda: "x you@example.org")
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params.get("type"))
        return httpx.Response(200, text=ATOM)

    edgar_rss.fetch(client=_client(handler))
    assert asked == list(edgar_rss.FORM_TYPES)


def test_the_watched_forms_are_the_ones_that_mean_something() -> None:
    """Deal detection keys on form type, not on news. These are the types
    that are unambiguous on their own."""
    assert set(edgar_rss.FORM_TYPES) == {
        "4", "8-K", "S-4", "DEFM14A", "SC 13D", "SC TO-T", "SC 13E-3",
    }


# --- pacing -------------------------------------------------------------


def test_the_pacer_stays_under_the_sec_ceiling() -> None:
    """SEC publishes 10 req/sec. Eight leaves headroom for anything else of
    ours that happens to be talking to them at the same moment."""
    assert edgar_rss.REQUESTS_PER_SECOND <= 10
    assert edgar_rss.Pacer(8.0).interval == pytest.approx(0.125)
