"""Offline coverage for the SEC loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from marketradar import manifest
from marketradar.sources import sec_company_tickers as sec
from marketradar.sources.sec_company_tickers import SecError

PAYLOAD = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1067983, "ticker": "BRK-A", "title": "BERKSHIRE HATHAWAY INC"},
    "2": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
    "3": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
    "4": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
}


# --- shaping --------------------------------------------------------------

def test_shape_produces_one_row_per_cik_ticker_pair() -> None:
    """Repetition of a CIK is the data, not a duplicate: it is share classes."""
    filers = sec.shape(PAYLOAD)
    assert len(filers) == 5
    assert len({f.cik for f in filers}) == 3


def test_ciks_are_zero_padded() -> None:
    assert {f.cik for f in sec.shape(PAYLOAD)} == {
        "0000320193", "0001067983", "0001652044"
    }


def test_names_are_normalized_for_fuzzy_matching() -> None:
    by_ticker = {f.ticker: f for f in sec.shape(PAYLOAD)}
    assert by_ticker["AAPL"].normalized == "apple"
    assert by_ticker["BRK-A"].normalized == "berkshire hathaway"


def test_share_classes_share_a_normalized_name() -> None:
    by_ticker = {f.ticker: f for f in sec.shape(PAYLOAD)}
    assert by_ticker["GOOG"].normalized == by_ticker["GOOGL"].normalized
    assert by_ticker["GOOG"].cik == by_ticker["GOOGL"].cik


def test_exact_duplicate_pairs_are_dropped() -> None:
    payload = dict(PAYLOAD)
    payload["5"] = {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}
    assert len(sec.shape(payload)) == 5


def test_rows_missing_required_fields_are_skipped() -> None:
    payload = {
        "0": {"cik_str": None, "ticker": "X", "title": "X"},
        "1": {"cik_str": 1, "ticker": "", "title": "Y"},
        "2": {"cik_str": 2, "ticker": "Z", "title": ""},
        "3": {"cik_str": 3, "ticker": "OK", "title": "Fine Inc"},
    }
    filers = sec.shape(payload)
    assert [f.ticker for f in filers] == ["OK"]


def test_shape_accepts_a_list_payload_too() -> None:
    assert len(sec.shape(list(PAYLOAD.values()))) == 5


# --- the User-Agent requirement -------------------------------------------

def test_missing_user_agent_is_refused(monkeypatch) -> None:
    monkeypatch.delenv(sec.ENV_USER_AGENT, raising=False)
    with pytest.raises(SecError, match="descriptive User-Agent"):
        sec.user_agent()


def test_placeholder_user_agent_is_refused(monkeypatch) -> None:
    monkeypatch.setenv(sec.ENV_USER_AGENT, "Market Radar (dummy@example.com)")
    with pytest.raises(SecError, match="real contact address"):
        sec.user_agent()


def test_user_agent_without_an_email_is_refused(monkeypatch) -> None:
    """SEC asks for a contact, not a product name."""
    monkeypatch.setenv(sec.ENV_USER_AGENT, "Market Radar")
    with pytest.raises(SecError, match="no email address"):
        sec.user_agent()


def test_a_real_user_agent_is_accepted(monkeypatch) -> None:
    monkeypatch.setenv(sec.ENV_USER_AGENT, "Market Radar someone@realdomain.net")
    assert sec.user_agent() == "Market Radar someone@realdomain.net"


# --- the licensing split, public direction --------------------------------

def test_publish_refuses_a_private_backend(tmp_path: Path, monkeypatch) -> None:
    """Mirror image of the Tiingo guard. Government data on R2 is not a
    licensing breach, but it is a mistake: it costs storage and hides public
    data behind credentials."""
    mf = tmp_path / "manifest.toml"
    mf.write_text(
        '[sec_company_tickers]\n'
        'all = { location = "r2://bucket/sec.parquet", backend = "r2" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(mf))
    manifest.clear_cache()

    with pytest.raises(SecError, match="public domain and belongs in a GitHub Release"):
        sec.publish(sec.shape(PAYLOAD))


def test_committed_manifest_puts_sec_data_on_a_public_backend() -> None:
    manifest.clear_cache()
    ref = manifest.get("sec_company_tickers", "all")
    assert ref.backend == "github_release"
    assert ref.is_private is False
    assert "example.invalid" not in ref.location, "still the placeholder location"


def test_min_rows_floor_is_meaningful() -> None:
    """SEC lists ~10,000 filers. A floor of 1 would pass an empty-ish file."""
    assert sec.MIN_ROWS >= 5_000


# --- batching -------------------------------------------------------------

def test_batching_splits_evenly() -> None:
    assert sec._batched([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert sec._batched([], 100) == []


def test_sql_quote_escaping() -> None:
    """Company names contain apostrophes -- Moody's, Macy's, Lowe's."""
    assert sec._q("Moody's Corp") == "Moody''s Corp"


def test_batch_size_is_large_enough_to_matter() -> None:
    """Row-by-row upserts meant 22,000 round trips and did not finish in ten
    minutes. The batch size is the fix, so it is asserted."""
    assert sec.BATCH_SIZE >= 100
