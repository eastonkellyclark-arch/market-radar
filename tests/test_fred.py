"""Offline coverage for the FRED loader. No network, no credentials."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from marketradar.sources import fred


# --- parsing ------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("4.77", Decimal("4.77")),
        ("0", Decimal("0")),
        ("-0.25", Decimal("-0.25")),
        (".", None),          # FRED's marker for a date with no print
        ("", None),
        (None, None),
        ("n/a", None),
    ],
)
def test_value_parsing(raw, expected) -> None:
    assert fred._parse_value(raw) == expected


def test_a_holiday_is_not_zero() -> None:
    """The distinction the macro line depends on.

    ``"."`` means the market was shut. Coercing it to 0.0 would put a
    zero-yield print in the series and drag every average through it.
    """
    assert fred._parse_value(".") is None
    assert fred._parse_value("0") == Decimal("0")


def test_date_parsing_survives_junk() -> None:
    assert fred._parse_date("2026-09-04") == date(2026, 9, 4)
    assert fred._parse_date("2026-09-04T00:00:00") == date(2026, 9, 4)
    assert fred._parse_date("not a date") is None
    assert fred._parse_date(None) is None


# --- credentials --------------------------------------------------------


def test_missing_api_key_is_named_not_inferred(monkeypatch) -> None:
    monkeypatch.delenv(fred.ENV_API_KEY, raising=False)
    with pytest.raises(fred.FredError, match=fred.ENV_API_KEY):
        fred.api_key()


# --- the licensing boundary --------------------------------------------


def test_publish_refuses_the_ice_series_by_default(monkeypatch) -> None:
    """The rule in CLAUDE.md, enforced in code rather than in a comment.

    DGS10 is Treasury data and ours to republish. The BAMLxxx series are ICE
    BofA indices FRED redistributes under permission; putting those in a
    public Release asset is redistributing a third-party index.
    """
    monkeypatch.setenv("MR_GITHUB_REPO", "someone/market-radar")
    observations = [
        fred.Observation("DGS10", date(2026, 9, 3), Decimal("4.77")),
        fred.Observation("BAMLH0A0HYM2", date(2026, 9, 7), Decimal("2.68")),
    ]
    with pytest.raises(fred.FredError, match="BAMLH0A0HYM2"):
        fred.publish(observations, con=object())


def test_publish_allows_the_treasury_series_through(monkeypatch) -> None:
    """Only the licensing guard is under test; the upload is not reached."""
    monkeypatch.setenv("MR_GITHUB_REPO", "someone/market-radar")
    monkeypatch.setattr(fred, "_gh", lambda: (_ for _ in ()).throw(
        fred.FredError("reached the upload")
    ))
    observations = [fred.Observation("DGS10", date(2026, 9, 3), Decimal("4.77"))]
    with pytest.raises(fred.FredError, match="reached the upload"):
        fred.publish(observations, con=object())


def test_every_series_declares_which_side_of_the_boundary_it_is_on() -> None:
    for series in fred.SERIES:
        assert isinstance(series.public_domain, bool)
    assert {s.series_id for s in fred.SERIES if s.public_domain} == {"DGS10"}


# --- freshness floors ---------------------------------------------------


def test_floors_are_per_series_because_the_histories_differ() -> None:
    """FRED serves DGS10 from 1962 but the BAML series only from 2023-09-11.

    One global floor would either fail the spreads on every run or be too
    slack to notice a truncated Treasury pull.
    """
    floors = {s.series_id: s.min_rows for s in fred.SERIES}
    assert floors["DGS10"] > floors["BAMLH0A0HYM2"]
    assert all(v > 0 for v in floors.values())


def test_staleness_window_survives_a_holiday_weekend() -> None:
    """DGS10 lands on the H.15 schedule, a day behind the spreads.

    Friday's print read after a Monday holiday is three days old with nothing
    wrong, and the H.15 lag adds one more.
    """
    assert fred.MAX_STALENESS_DAYS >= 5


def test_module_calls_assert_fresh() -> None:
    """Mirrors the repo invariant, close to the code it constrains."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(fred))
    assert any(
        isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", None)) == "assert_fresh"
        for n in ast.walk(tree)
    )
