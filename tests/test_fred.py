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


def test_there_is_no_publish_path_at_all() -> None:
    """FRED is local-only, and the absence is the enforcement.

    FRED carries the ICE BofA series under permission from ICE Data Indices,
    LLC, so they are not ours to mirror to a world-readable Release. A flag
    guarding that would be a flag someone later flips; no function and no
    flag cannot be flipped. Decided 2026-09-08, see CLAUDE.md.
    """
    assert not hasattr(fred, "publish")
    source = __import__("inspect").getsource(fred)
    assert "allow_licensed" not in source
    assert "github_release" not in source


def test_the_manifest_keeps_fred_out_of_a_public_backend() -> None:
    from marketradar import manifest

    ref = manifest.get("fred_series", "all")
    assert ref.backend == "supabase"
    assert ref.is_private


def test_every_series_declares_which_side_of_the_boundary_it_is_on() -> None:
    """Documentation, not logic — but the distinction has to stay recorded.

    Anyone adding a publish path later needs to see that "from a government
    source" and "ours to republish" are different tests.
    """
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
