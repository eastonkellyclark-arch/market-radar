"""Offline coverage for the Tiingo loader.

The sweep needs the network and real quota. Shaping, pacing, backoff, and the
licensing guard do not — and those are where the decisions live.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from marketradar import manifest
from marketradar.sources import tiingo
from marketradar.sources.tiingo import Pacer, TickerMeta, TiingoError, _backoff_seconds

META = TickerMeta("AAPL", "NASDAQ", "stock", date(1980, 12, 12), date(2026, 9, 4))

BAR = {
    "date": "2026-09-04T00:00:00.000Z",
    "open": 0.0002,
    "high": 0.0003,
    "low": 0.0001,
    "close": 0.00025,
    "volume": 123456,
    "adjOpen": 0.0001,
    "adjHigh": 0.00015,
    "adjLow": 0.00005,
    "adjClose": 0.000125,
    "adjVolume": 246912,
    "divCash": 0.0,
    "splitFactor": 1.0,
}


# --- shaping: raw only ----------------------------------------------------

def test_adjusted_columns_are_discarded() -> None:
    """Storing adjusted prices is what would make history mutable."""
    prices, _ = tiingo.shape_rows("AAPL", META, [BAR])
    assert prices
    for row in prices:
        assert not any(k.lower().startswith("adj") for k in row)


def test_raw_values_are_kept_not_adjusted_ones() -> None:
    prices, _ = tiingo.shape_rows("AAPL", META, [BAR])
    row = prices[0]
    assert row["close"] == Decimal("0.000250")     # raw close
    assert row["close"] != Decimal("0.000125")     # not adjClose


def test_sub_penny_precision_survives() -> None:
    """$0.0002 is why prices are Decimal(18,6) and not integer cents."""
    prices, _ = tiingo.shape_rows("AAPL", META, [BAR])
    row = prices[0]
    assert row["open"] == Decimal("0.000200")
    assert all(isinstance(row[c], Decimal) for c in ("open", "high", "low", "close"))


def test_metadata_is_attached() -> None:
    prices, _ = tiingo.shape_rows("AAPL", META, [BAR])
    assert prices[0]["exchange"] == "nasdaq"
    assert prices[0]["security_type"] == "stock"
    assert prices[0]["source"] == "tiingo"


def test_dates_become_date_objects() -> None:
    prices, _ = tiingo.shape_rows("AAPL", META, [BAR])
    assert prices[0]["date"] == date(2026, 9, 4)
    assert isinstance(prices[0]["date"], date)


def test_rows_with_unparseable_dates_are_skipped() -> None:
    prices, _ = tiingo.shape_rows("AAPL", META, [{**BAR, "date": "not-a-date"}])
    assert prices == []


# --- shaping: corporate actions -------------------------------------------

def test_a_plain_bar_produces_no_corporate_action() -> None:
    _, actions = tiingo.shape_rows("AAPL", META, [BAR])
    assert actions == []


def test_a_split_produces_a_corporate_action() -> None:
    _, actions = tiingo.shape_rows("AAPL", META, [{**BAR, "splitFactor": 0.05}])
    assert len(actions) == 1
    assert actions[0]["split_factor"] == Decimal("0.05")
    assert actions[0]["ticker"] == "AAPL"
    assert actions[0]["ex_date"] == date(2026, 9, 4)


def test_a_dividend_produces_a_corporate_action() -> None:
    _, actions = tiingo.shape_rows("AAPL", META, [{**BAR, "divCash": 0.24}])
    assert len(actions) == 1
    assert actions[0]["div_cash"] == Decimal("0.24")


def test_reverse_split_is_captured_precisely() -> None:
    """Reverse splits are constant in the sub-$1 band and are the single
    biggest source of fake signals if lost."""
    _, actions = tiingo.shape_rows("PENNY", META, [{**BAR, "splitFactor": 0.0333333}])
    assert actions[0]["split_factor"] == Decimal("0.0333333")


# --- pacing and backoff ---------------------------------------------------

def test_pacer_interval_matches_the_hourly_budget() -> None:
    assert Pacer(9000).interval == pytest.approx(0.4)
    assert Pacer(3600).interval == pytest.approx(1.0)


def test_pacer_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        Pacer(0)


def test_backoff_honours_retry_after() -> None:
    """A fixed sleep ignores what the server actually told us."""
    assert _backoff_seconds(0, "30") == 30.0


def test_backoff_caps_absurd_retry_after() -> None:
    assert _backoff_seconds(0, "99999") == 300.0


def test_backoff_ignores_malformed_retry_after() -> None:
    assert _backoff_seconds(0, "soon") < 3.0


def test_backoff_is_exponential_and_jittered() -> None:
    """Without jitter every stalled request retries at the same instant and
    the burst re-triggers the limit."""
    a = [_backoff_seconds(1, None) for _ in range(20)]
    b = [_backoff_seconds(4, None) for _ in range(20)]
    assert min(b) > max(a)
    assert len(set(a)) > 1


def test_backoff_is_capped() -> None:
    assert _backoff_seconds(30, None) <= 61.0


# --- request handling -----------------------------------------------------

@pytest.fixture
def no_sleep(monkeypatch):
    """Record backoff delays instead of serving them.

    The retry logic is what is under test; actually sleeping through it just
    makes the suite slow, and a slow suite gets skipped. The recorded delays
    are still asserted, so the pacing is not going untested.
    """
    slept: list[float] = []
    monkeypatch.setattr(tiingo.time, "sleep", lambda s: slept.append(s))
    return slept


def _client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://example.invalid"
    )


def test_429_is_retried_then_succeeds(no_sleep) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json=[BAR])

    result = tiingo.SweepResult()
    with _client(handler) as client:
        out = tiingo.fetch_prices(
            client, "AAPL", date(2026, 9, 1), date(2026, 9, 4), result, Pacer(3_600_000)
        )
    assert len(out) == 1
    assert calls["n"] == 3
    assert result.rate_limited == 2
    assert result.retries == 2
    # Retry-After was honoured rather than a fixed sleep being used. The
    # list also holds sub-millisecond Pacer waits, so count the backoffs.
    assert no_sleep.count(1.0) == 2


def test_404_is_not_an_error_just_no_data(no_sleep) -> None:
    result = tiingo.SweepResult()
    with _client(lambda r: httpx.Response(404)) as client:
        out = tiingo.fetch_prices(
            client, "GONE", date(2026, 9, 1), date(2026, 9, 4), result, Pacer(3_600_000)
        )
    assert out == []
    assert result.failed == 0


def test_persistent_429_eventually_raises(no_sleep) -> None:
    result = tiingo.SweepResult()
    with _client(lambda r: httpx.Response(429, headers={"Retry-After": "1"})) as client:
        with pytest.raises(TiingoError, match="still rate-limited"):
            tiingo.fetch_prices(
                client, "AAPL", date(2026, 9, 1), date(2026, 9, 4), result, Pacer(3_600_000)
            )
    assert no_sleep.count(1.0) == tiingo.MAX_RETRIES


def test_client_error_raises_immediately() -> None:
    result = tiingo.SweepResult()
    with _client(lambda r: httpx.Response(403, text="forbidden")) as client:
        with pytest.raises(TiingoError, match="HTTP 403"):
            tiingo.fetch_prices(
                client, "AAPL", date(2026, 9, 1), date(2026, 9, 4), result, Pacer(3_600_000)
            )


# --- the licensing guard --------------------------------------------------

def test_publish_refuses_a_public_backend(tmp_path: Path, monkeypatch) -> None:
    """Vendor data must never reach a GitHub Release. Guarded at runtime as
    well as by the manifest test, because the manifest test only sees the
    committed file."""
    mf = tmp_path / "manifest.toml"
    mf.write_text(
        '[prices_eod_raw]\n'
        '2026 = { location = "https://example.invalid/p.parquet", '
        'backend = "github_release" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(mf))
    manifest.clear_cache()

    with pytest.raises(TiingoError, match="publicly readable"):
        tiingo.publish(tmp_path, "2026", min_rows=1)


def test_publish_refuses_when_nothing_was_staged(tmp_path: Path, monkeypatch) -> None:
    """An empty staging dir must not silently publish an empty partition."""
    mf = tmp_path / "manifest.toml"
    mf.write_text(
        '[prices_eod_raw]\n'
        '2026 = { location = "r2://b/p.parquet", backend = "r2" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(mf))
    manifest.clear_cache()

    with pytest.raises(TiingoError, match="nothing to publish"):
        tiingo.publish(tmp_path, "2026", min_rows=1)


# --- credentials ----------------------------------------------------------

def test_missing_token_is_a_clear_error(monkeypatch) -> None:
    monkeypatch.delenv(tiingo.ENV_TOKEN, raising=False)
    with pytest.raises(TiingoError, match="source of record"):
        tiingo._token()


def test_placeholder_token_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv(tiingo.ENV_TOKEN, "dummy-tiingo-api-key")
    with pytest.raises(TiingoError, match="placeholder"):
        tiingo._token()


# --- universe filtering ---------------------------------------------------

def test_listed_exchanges_exclude_otc() -> None:
    """Stooq's US bulk is listed-only and Tiingo OTC coverage is unverified.
    Keeping OTC out of the universe is deliberate, not an oversight."""
    assert "OTC" not in tiingo.LISTED_EXCHANGES
    assert "PINK" not in tiingo.LISTED_EXCHANGES
    assert "NASDAQ" in tiingo.LISTED_EXCHANGES


# --- universe filtering rules ---------------------------------------------

CUTOFF = date(2026, 8, 8)  # 30 days before 2026-09-07


def _row(**kw):
    base = {
        "ticker": "AAPL",
        "exchange": "NASDAQ",
        "assetType": "Stock",
        "startDate": "1980-12-12",
        "endDate": "2026-09-04",
    }
    return {**base, **kw}


def test_listed_row_is_included() -> None:
    assert tiingo.include_row(_row(), CUTOFF) is True


def test_nyse_american_is_kept_under_both_codes() -> None:
    """Tiingo splits NYSE American across AMEX and NYSE MKT. A low count on
    one code is a labelling artefact, not a dropped exchange."""
    assert tiingo.include_row(_row(exchange="AMEX"), CUTOFF) is True
    assert tiingo.include_row(_row(exchange="NYSE MKT"), CUTOFF) is True


def test_otc_is_excluded_but_the_codes_are_recorded() -> None:
    """Excluding OTC is a decision, not an unknown -- Tiingo does carry it."""
    for code in ("PINK", "OTCMKTS", "OTCQB", "OTCGREY"):
        assert tiingo.include_row(_row(exchange=code), CUTOFF) is False
        assert code in tiingo.OTC_EXCHANGES


def test_foreign_exchanges_are_excluded() -> None:
    for code in ("SHE", "SHG"):
        assert tiingo.include_row(_row(exchange=code), CUTOFF) is False


def test_exchange_test_symbols_are_excluded() -> None:
    """Every venue publishes live-looking rows that are not securities.

    Regression with a date on it: on 2026-09-04, ZVZZT reached the screens and
    took the top of the liquid $10+ loser list at -86.75% on a fabricated
    $7.6M average dollar volume. The filter covered NYSE's family only.
    """
    nyse = ("ATEST", "ATEST-A", "ATEST-B", "ATEST-Z")
    nasdaq = ("ZAZZT", "ZBZZT", "ZCZZT", "ZJZZT", "ZVZZT", "ZWZZT", "ZXZZT")
    cboe = ("ZTEST", "TEST")

    for t in nyse:
        assert tiingo.include_row(_row(ticker=t, exchange="NYSE MKT"), CUTOFF) is False
    for t in nasdaq:
        assert tiingo.include_row(_row(ticker=t, exchange="NASDAQ"), CUTOFF) is False
    for t in cboe:
        assert tiingo.include_row(_row(ticker=t, exchange="BATS"), CUTOFF) is False


def test_real_tickers_that_merely_start_like_test_symbols_survive() -> None:
    """The reason this is a pattern and not a prefix list.

    Tiingo carries TESTF, TESTJ, TESTK, TESTM, TESTS, TESTT and TESTY. They
    are OTC today and excluded by exchange, but a bare ``startswith("TEST")``
    would silently drop all seven the moment the OTC band is switched on.
    """
    for t in ("TESTF", "TESTJ", "TESTM", "TESTY", "ZAZZTX", "ATESTING"):
        assert tiingo.include_row(_row(ticker=t, exchange="NASDAQ"), CUTOFF) is True


def test_non_equity_asset_types_are_excluded() -> None:
    assert tiingo.include_row(_row(assetType="Mutual Fund"), CUTOFF) is False


def test_delisted_symbol_is_excluded() -> None:
    assert tiingo.include_row(_row(endDate="2019-04-01"), CUTOFF) is False


def test_missing_end_date_is_excluded_when_filtering() -> None:
    assert tiingo.include_row(_row(endDate=""), CUTOFF) is False


def test_no_cutoff_keeps_delisted_symbols_for_backfill() -> None:
    assert tiingo.include_row(_row(endDate="2001-01-01"), None) is True


# --- the halt/suspension question -----------------------------------------

def test_active_window_covers_an_sec_trading_suspension() -> None:
    """SEC suspensions run 10 business days, about 14 calendar days. A 7-day
    window would drop a suspended name mid-suspension."""
    assert tiingo.ACTIVE_WITHIN_DAYS >= 14


def test_a_suspended_ticker_survives_the_whole_suspension() -> None:
    today = date(2026, 9, 7)
    cutoff = today - timedelta(days=tiingo.ACTIVE_WITHIN_DAYS)
    halted_on = today - timedelta(days=14)  # full SEC suspension, still halted
    assert tiingo.include_row(_row(endDate=halted_on.isoformat()), cutoff) is True


def test_a_resumed_ticker_returns_to_the_universe() -> None:
    """The filter is recomputed from a freshly downloaded universe each run,
    so removal is never permanent: once the symbol trades again its endDate
    moves forward and it is back in scope."""
    today = date(2026, 9, 7)
    cutoff = today - timedelta(days=tiingo.ACTIVE_WITHIN_DAYS)

    long_gone = _row(endDate=(today - timedelta(days=60)).isoformat())
    assert tiingo.include_row(long_gone, cutoff) is False

    resumed = {**long_gone, "endDate": today.isoformat()}
    assert tiingo.include_row(resumed, cutoff) is True


def test_non_terminating_split_ratios_survive_the_stored_scale() -> None:
    """A 1-for-15 reverse split arrives as 0.0666666667 -- ten decimal places.

    The corporate-action columns were decimal128(18,8) and pyarrow refused
    these outright ("Rescaling Decimal value would cause data loss"), which is
    the right instinct in the wrong place: it only surfaces once the history
    is deep enough to contain such a split, so a five-day sweep looked fine
    for months and a ten-year backfill died on ticker 40.
    """
    from decimal import Decimal

    for raw in (0.0666666667, 0.0555555556, 0.04873009, 0.1679969):
        got = tiingo._factor(raw, Decimal(1))
        assert -got.as_tuple().exponent <= 12, f"{raw} kept too many places"

    assert tiingo._factor(None, Decimal(1)) == Decimal(1)
    assert tiingo._factor(None, Decimal(0)) == Decimal(0)
    assert tiingo._factor("not a number", Decimal(1)) == Decimal(1)


def test_a_one_for_fifteen_reverse_split_still_adjusts_correctly() -> None:
    """Quantising must not move the adjusted price at the stored precision."""
    from decimal import Decimal

    factor = tiingo._factor(0.0666666667, Decimal(1))
    adjusted = (Decimal("0.05") / factor).quantize(Decimal("0.000001"))
    assert adjusted == Decimal("0.750000")


# --- corporate actions: the upsert that reported what it hoped for -------


class _StubCon:
    """Enough of a DuckDB connection to drive upsert_corporate_actions.

    ``missing`` is what the verification anti-join will report.
    """

    def __init__(self, rows, missing):
        self.rows = rows
        self.missing = missing
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        if params and isinstance(params[0], str) and "insert into" in params[0]:
            self.statements.append(params[0])
            self._result = None
        elif "ANTI JOIN" in sql:
            self._result = (self.missing,)
        elif "SELECT DISTINCT" in sql:
            self._result = self.rows
        else:
            self._result = None
        return self

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self._result


def _staged_actions(tmp_path, n):
    """A staging directory with one actions parquet, via DuckDB itself."""
    import duckdb

    staging = tmp_path / "staging"
    staging.mkdir()
    con = duckdb.connect()
    con.execute(
        "create table a (ticker text, ex_date date, split_factor double, "
        "div_cash double, source text)"
    )
    con.executemany(
        "insert into a values (?, ?, ?, ?, 'tiingo')",
        [(f"T{i}", date(2023, 1, 6), 0.05, 0.0) for i in range(n)],
    )
    out = (staging / "actions_00000.parquet").as_posix()
    con.execute(f"copy a to '{out}' (format parquet)")
    return staging


def test_upsert_batches_instead_of_one_round_trip_per_row(tmp_path, monkeypatch):
    """One statement per row cost this table 90% of its contents: a 10-year
    backfill stages ~254,000 actions and that many round trips do not
    finish."""
    from marketradar import storage
    from marketradar.sources import tiingo as t

    monkeypatch.setattr(storage, "postgres_attached", lambda con: True)
    staging = _staged_actions(tmp_path, 3)
    rows = [(f"T{i}", date(2023, 1, 6), 0.05, 0.0, "tiingo") for i in range(1200)]
    con = _StubCon(rows, missing=0)

    assert t.upsert_corporate_actions(staging, con=con) == 1200
    # 1200 rows at 500 per statement is three, not 1200.
    assert len(con.statements) == 3
    assert all(s.count("(") > 100 for s in con.statements)


def test_upsert_raises_when_rows_did_not_land(tmp_path, monkeypatch):
    """The original returned len(rows) whatever happened, so a run that wrote
    a tenth of its rows reported complete success. corporate_actions ended up
    with 365 splits where the parquet held 3,724, and the volatility screens
    adjusted from it every morning."""
    from marketradar import storage
    from marketradar.sources import tiingo as t

    monkeypatch.setattr(storage, "postgres_attached", lambda con: True)
    staging = _staged_actions(tmp_path, 3)
    rows = [("AYTU", date(2023, 1, 6), 0.05, 0.0, "tiingo")]
    con = _StubCon(rows, missing=1)

    with pytest.raises(t.TiingoError) as exc:
        t.upsert_corporate_actions(staging, con=con)
    assert "not in corporate_actions" in str(exc.value)


def test_upsert_with_no_staged_actions_is_zero(tmp_path):
    from marketradar.sources import tiingo as t

    empty = tmp_path / "empty"
    empty.mkdir()
    assert t.upsert_corporate_actions(empty, con=None) == 0
