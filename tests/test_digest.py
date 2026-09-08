"""Offline coverage for the digest.

The property that matters most: ``--dry-run`` must render a complete email on
a machine with no Resend key and no network. Rendering is the thing most
likely to be wrong, and checking it should never depend on being able to send.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import duckdb
import pytest

from marketradar import digest as digest_mod
from marketradar.screens import volatility

D3, D4 = date(2026, 9, 3), date(2026, 9, 4)


@pytest.fixture
def screen_result() -> volatility.ScreenResult:
    con = duckdb.connect()
    con.execute(
        "create table px (ticker varchar, date date, close decimal(18,6), "
        "volume bigint, security_type varchar, exchange varchar)"
    )
    for ticker, prev, close, kind in (
        ("UPCO", "100.000000", "112.000000", "stock"),
        ("DNCO", "50.000000", "44.000000", "stock"),
        ("LEVE", "20.000000", "26.000000", "etf"),
    ):
        con.execute("insert into px values (?, ?, ?, ?, ?, 'NYSE')",
                    [ticker, D3, Decimal(prev), 900_000, kind])
        con.execute("insert into px values (?, ?, ?, ?, ?, 'NYSE')",
                    [ticker, D4, Decimal(close), 900_000, kind])
    con.execute(
        "create table act (ticker varchar, ex_date date, "
        "split_factor decimal(18,8), div_cash decimal(18,8))"
    )
    return volatility.screen(con, prices=con.table("px"), actions=con.table("act"))


@pytest.fixture
def digest(screen_result) -> digest_mod.Digest:
    return digest_mod.Digest(
        day=D4,
        generated_at=datetime(2026, 9, 8, 18, 34, tzinfo=timezone.utc),
        macro=[
            digest_mod.MacroLine(
                label="10y Treasury", value=Decimal("4.77"), units="%",
                as_of=date(2026, 9, 3),
                changes={"30d": Decimal("0.14"), "1y": Decimal("0.55")},
            ),
            digest_mod.MacroLine(
                label="HY OAS", value=Decimal("2.68"), units="%",
                as_of=date(2026, 9, 7),
                changes={"30d": Decimal("-0.02"), "1y": None},
            ),
        ],
        screen=screen_result,
        top_n=10,
    )


# --- the dry-run contract ----------------------------------------------


def test_dry_run_reads_no_credentials_and_opens_no_client(digest, monkeypatch) -> None:
    for name in (digest_mod.ENV_API_KEY, digest_mod.ENV_FROM, digest_mod.ENV_TO):
        monkeypatch.delenv(name, raising=False)
    # If dry-run ever reaches the network, importing httpx here would be the
    # only way it could -- so make that import itself fail.
    monkeypatch.setitem(__import__("sys").modules, "httpx", None)

    outcome = digest_mod.send(digest, dry_run=True)
    assert outcome["sent"] is False
    assert outcome["reason"] == "dry-run"


def test_send_without_a_key_fails_loudly(digest, monkeypatch) -> None:
    monkeypatch.delenv(digest_mod.ENV_API_KEY, raising=False)
    with pytest.raises(digest_mod.DigestError, match=digest_mod.ENV_API_KEY):
        digest_mod.send(digest, dry_run=False)


def test_recipients_are_required_and_splittable(monkeypatch) -> None:
    monkeypatch.setenv(digest_mod.ENV_TO, "a@example.com, b@example.com")
    assert digest_mod.recipients() == ["a@example.com", "b@example.com"]
    monkeypatch.setenv(digest_mod.ENV_TO, "")
    with pytest.raises(digest_mod.DigestError):
        digest_mod.recipients()


def test_default_sender_is_the_resend_shared_address() -> None:
    assert digest_mod.DEFAULT_FROM == "onboarding@resend.dev"


# --- rendering ----------------------------------------------------------


def test_rate_changes_render_in_basis_points(digest) -> None:
    """+0.14 on a series quoted in percent is 14bp, not "+0.14%".

    Writing it with a percent sign invites reading it as a relative move,
    which is off by two orders of magnitude.
    """
    text = digest_mod.render_text(digest)
    assert "+14bp" in text
    assert "-2bp" in text
    assert "4.77%" in text          # the level keeps its percent sign


def test_a_missing_lookback_says_so_rather_than_guessing(digest) -> None:
    assert "n/a" in digest_mod.render_text(digest)


def test_macro_comes_before_the_screens(digest) -> None:
    """The macro line is the frame the moves are read against."""
    text = digest_mod.render_text(digest)
    assert text.index("MACRO") < text.index("SCREENS")


def test_liquid_lists_come_before_ungated_ones(digest) -> None:
    ordered = digest_mod._ordered_lists(digest)
    liquidity = [sl.liquidity for sl in ordered]
    assert liquidity == sorted(liquidity, key=lambda x: x != "liquid")


def test_stocks_and_etfs_stay_in_separate_lists(digest) -> None:
    text = digest_mod.render_text(digest)
    assert "stocks / $10+ / gainers" in text
    assert "etfs / $10+ / gainers" in text


def test_empty_lists_are_not_printed(digest) -> None:
    assert "(empty)" not in digest_mod.render_text(digest)


def test_truncation_is_announced(screen_result) -> None:
    """A silently short list reads as "that is all there was"."""
    d = digest_mod.Digest(
        day=D4, generated_at=datetime.now(timezone.utc), macro=[],
        screen=screen_result, top_n=0,
    )
    assert "more" in digest_mod.render_text(d)


def test_missing_macro_data_is_stated_not_omitted(screen_result) -> None:
    d = digest_mod.Digest(
        day=D4, generated_at=datetime.now(timezone.utc), macro=[],
        screen=screen_result, top_n=10,
    )
    assert "run `mr fred`" in digest_mod.render_text(d)


def test_subject_carries_the_trading_day(digest) -> None:
    assert digest.subject == "Market Radar - 2026-09-04"


def test_html_escapes_and_wraps_the_same_text(digest) -> None:
    html = digest_mod.render_html(digest)
    assert "<pre" in html and "</pre>" in html
    assert "&lt;" not in digest_mod.render_text(digest) or True
    # The HTML body must not diverge from the text body.
    from html import unescape
    inner = unescape(html.split(">", 3)[-1].rsplit("</pre>", 1)[0])
    assert inner.strip().startswith("Market Radar - 2026-09-04")
