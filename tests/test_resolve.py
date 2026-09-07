"""Name normalisation and the join rules.

Normalisation is being built against SEC names, which are clean, so that the
Form 5500 sponsor names in Weekend 4 — DBAs, legal entities, subsidiary
rollups — have a stable target to match against.
"""

from __future__ import annotations

import pytest

from marketradar.entities.resolve import (
    TickerMatch,
    is_ambiguous,
    normalize,
    normalize_cik,
    normalize_ticker,
)


# --- name normalisation ---------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Apple Inc.", "apple"),
        ("APPLE INC", "apple"),
        ("Berkshire Hathaway Inc.", "berkshire hathaway"),
        ("The Coca-Cola Company", "coca cola"),
        ("Acme Holdings, LLC", "acme"),
        ("Acme Holdings Inc", "acme"),
        ("Ford Motor Co", "ford motor"),
        ("Smith & Wesson Brands, Inc.", "smith and wesson brands"),
        ("Vanguard Group Ltd.", "vanguard"),
        ("BLACKROCK, INC.", "blackrock"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_normalize_strips_stacked_suffixes() -> None:
    """'Holdings Corp' is two suffixes, not one."""
    assert normalize("Widget Holdings Corp") == "widget"


def test_normalize_never_returns_empty_for_a_real_name() -> None:
    """'Holdings Inc' is a real Form 5500 sponsor name. Reducing it to '' would
    make it match every other company in the file."""
    assert normalize("Holdings Inc") != ""
    assert normalize("Inc") != ""
    assert normalize("The Company") != ""


def test_normalize_handles_accents() -> None:
    assert normalize("Société Générale SA") == "societe generale"


def test_normalize_is_idempotent() -> None:
    once = normalize("The Acme Holdings Company, LLC")
    assert normalize(once) == once


def test_normalize_of_empty_is_empty() -> None:
    assert normalize("") == ""


def test_share_classes_normalize_together() -> None:
    """BRK.A and BRK.B are one company; their names must collapse to one key."""
    assert normalize("BERKSHIRE HATHAWAY INC") == normalize("Berkshire Hathaway Inc.")


def test_different_companies_do_not_collide() -> None:
    assert normalize("Apple Inc.") != normalize("Apple Hospitality REIT, Inc.")


# --- CIK ------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        (320193, "0000320193"),
        ("320193", "0000320193"),
        ("0000320193", "0000320193"),
        ("CIK0000320193", "0000320193"),
        (None, None),
        ("", None),
        ("not-a-cik", None),
    ],
)
def test_normalize_cik(raw, expected) -> None:
    """SEC publishes CIKs as ints in some files and padded strings in others.
    Storing both forms means half the joins silently miss."""
    assert normalize_cik(raw) == expected


def test_cik_forms_converge() -> None:
    assert normalize_cik(320193) == normalize_cik("0000320193") == normalize_cik("320193")


# --- ticker ---------------------------------------------------------------

def test_normalize_ticker_uppercases_and_trims() -> None:
    assert normalize_ticker("  aapl ") == "AAPL"
    assert normalize_ticker(None) is None
    assert normalize_ticker("  ") is None


def test_ambiguity_detection() -> None:
    """Recycled tickers produce more than one company for one string."""
    a = TickerMatch(1, "0000000001", "XYZ", "Old Corp", "sec")
    b = TickerMatch(2, "0000000002", "XYZ", "New Corp", "sec")
    assert is_ambiguous([a, b]) is True
    assert is_ambiguous([a]) is False
    assert is_ambiguous([]) is False


def test_share_classes_of_one_company_are_not_ambiguous() -> None:
    """Two tickers, one company_id -- that is share classes, not ambiguity."""
    a = TickerMatch(7, "0001067983", "BRK-A", "Berkshire", "sec")
    b = TickerMatch(7, "0001067983", "BRK-B", "Berkshire", "sec")
    assert is_ambiguous([a, b]) is False


def test_no_function_maps_a_ticker_to_a_single_company() -> None:
    """The enforcement is the API shape: there is deliberately no
    company_id_for_ticker(). Writing one would be writing the bug."""
    from marketradar.entities import resolve

    assert not hasattr(resolve, "company_id_for_ticker")
    assert hasattr(resolve, "company_ids_for_ticker")
