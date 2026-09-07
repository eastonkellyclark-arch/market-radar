"""Offline coverage for the selftest's own logic.

The full round trip needs R2, a GitHub Release, and a network. These tests
cover everything up to that boundary so the pieces are exercised on every
commit, including on a machine with no credentials at all.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from marketradar import manifest, selftest, storage
from marketradar.freshness import StaleDataError, assert_fresh
from marketradar.selftest import SelftestError

TODAY = date(2026, 9, 6)


# --- synthetic data -------------------------------------------------------

def test_generate_is_decimal_18_6_with_a_sub_penny_value(con) -> None:
    """The precision path, end to end. $0.0002 is why prices are not cents."""
    rel = selftest.generate(con, stale=False, today=TODAY)
    lo, kind = rel.query("rel", "SELECT min(close), typeof(min(close)) FROM rel").fetchone()
    assert kind.upper() == "DECIMAL(18,6)"
    assert lo == Decimal("0.000200")
    assert isinstance(lo, Decimal)


def test_generate_produces_fresh_data_that_passes(con) -> None:
    rel = selftest.generate(con, stale=False, today=TODAY)
    observed = assert_fresh(
        "x", rel, min_rows=selftest.MIN_ROWS, expect_cols=("ticker", "close"), today=TODAY
    )
    assert observed.row_count == selftest.ROWS
    assert observed.max_date == TODAY


def test_generate_stale_is_rejected(con) -> None:
    """The injector must produce data the assertion actually refuses."""
    rel = selftest.generate(con, stale=True, today=TODAY)
    with pytest.raises(StaleDataError, match="feed has probably stopped"):
        assert_fresh("x", rel, min_rows=selftest.MIN_ROWS, today=TODAY)


def test_staleness_injection_clears_the_window_by_a_wide_margin() -> None:
    """45 days, not 5 — no holiday or long weekend can excuse it."""
    from marketradar.freshness import DEFAULT_MAX_STALENESS_DAYS

    assert selftest.STALENESS_INJECTION_DAYS > DEFAULT_MAX_STALENESS_DAYS * 5


def test_generate_spans_all_three_price_bands(con) -> None:
    """Sub-$1, $1-10, and $10+ all present, so band logic has real inputs."""
    rel = selftest.generate(con, stale=False, today=TODAY)
    bands = rel.query(
        "rel",
        """
        SELECT count(*) FILTER (WHERE close < 1)                     AS sub1,
               count(*) FILTER (WHERE close >= 1 AND close < 10)     AS mid,
               count(*) FILTER (WHERE close >= 10)                   AS high
        FROM rel
        """,
    ).fetchone()
    assert all(b > 0 for b in bands), f"missing a price band: {bands}"


# --- targets and the licensing split --------------------------------------

def test_build_targets_exercises_both_backends(monkeypatch) -> None:
    monkeypatch.setenv("MR_GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("MR_R2_BUCKET", "test-bucket")
    targets = selftest.build_targets("run-1")

    by_backend = {t.backend: t for t in targets}
    assert set(by_backend) == {"r2", "github_release"}


def test_vendor_data_goes_private_and_gov_data_goes_public(monkeypatch) -> None:
    """The licensing boundary, checked on the actual targets."""
    monkeypatch.setenv("MR_GITHUB_REPO", "owner/repo")
    targets = {t.dataset: t for t in selftest.build_targets("run-1")}

    prices = targets[selftest.SELFTEST_PRICES]
    assert prices.backend in manifest.PRIVATE_BACKENDS
    assert prices.location.startswith("r2://")

    gov = targets[selftest.SELFTEST_GOV]
    assert gov.backend not in manifest.PRIVATE_BACKENDS
    assert gov.location.startswith("https://github.com/")


def test_release_target_points_at_a_real_release_url(monkeypatch) -> None:
    """Guards the bug where the 'Release' target held an r2:// location and
    the backend column was simply a lie."""
    monkeypatch.setenv("MR_GITHUB_REPO", "owner/repo")
    gov = {t.dataset: t for t in selftest.build_targets("r")}[selftest.SELFTEST_GOV]
    assert f"/releases/download/{selftest.RELEASE_TAG}/" in gov.location
    assert "r2://" not in gov.location


def test_missing_github_repo_is_a_clear_error(monkeypatch) -> None:
    monkeypatch.delenv("MR_GITHUB_REPO", raising=False)
    with pytest.raises(SelftestError, match="MR_GITHUB_REPO is unset"):
        selftest.build_targets("run-1")


def test_written_manifest_round_trips(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MR_GITHUB_REPO", "owner/repo")
    targets = selftest.build_targets("run-1")
    path = tmp_path / "manifest.toml"
    selftest.write_manifest(path, targets)

    monkeypatch.setenv(manifest.OVERRIDE_ENV, str(path))
    manifest.clear_cache()
    for t in targets:
        ref = manifest.get(t.dataset, t.partition)
        assert ref.location == t.location
        assert ref.backend == t.backend


# --- degrading without Supabase -------------------------------------------

def test_postgres_configured_is_false_when_unset(monkeypatch) -> None:
    monkeypatch.delenv(storage.ENV_PG_DSN, raising=False)
    assert storage.postgres_configured() is False


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://dummy:dummy@localhost:5432/postgres",
        "postgresql://user:changeme@example.com:5432/db",
        "postgresql://your-user:pw@host:5432/db",
    ],
)
def test_placeholder_dsn_counts_as_unconfigured(dsn: str, monkeypatch) -> None:
    """A .env copied from .env.example must not look like a live database.

    Without this, every fresh checkout tries to attach Postgres at localhost
    and dies before doing any work.
    """
    monkeypatch.setenv(storage.ENV_PG_DSN, dsn)
    assert storage.postgres_configured() is False


def test_real_looking_dsn_counts_as_configured(monkeypatch) -> None:
    # Must look genuinely real to be a meaningful test, so it is explicitly
    # allowlisted for the pre-commit hook. Not a live credential.
    monkeypatch.setenv(
        storage.ENV_PG_DSN,
        "postgresql://postgres.abcd:s3cret@aws-0-us-east-1.pooler.supabase.com:5432/postgres",  # pragma: allowlist secret
    )
    assert storage.postgres_configured() is True


def test_connect_degrades_when_dsn_is_a_placeholder(monkeypatch) -> None:
    """Covered code path, not a crash: the selftest's step 4 skip."""
    monkeypatch.setenv(storage.ENV_PG_DSN, "postgresql://dummy:dummy@localhost:5432/x")
    con = storage.connect(enable_http=False)
    assert storage.postgres_attached(con) is False

    obs = manifest.Freshness(selftest.SELFTEST_PRICES, "2026", 5_000, TODAY)
    assert manifest.record_stats(obs, con=con) is False


def test_explicit_attach_still_raises(monkeypatch) -> None:
    """Auto-detect degrades; an explicit request must not fail silently."""
    monkeypatch.delenv(storage.ENV_PG_DSN, raising=False)
    with pytest.raises(storage.StorageError, match="MR_POSTGRES_DSN is not set"):
        storage.connect(enable_http=False, attach_postgres=True)


# --- .env loading ---------------------------------------------------------

def test_load_dotenv_never_overrides_a_real_env_var(tmp_path: Path, monkeypatch) -> None:
    """Actions secrets must never be shadowed by a stray local file."""
    from marketradar.cli import load_dotenv

    env = tmp_path / ".env"
    env.write_text("MR_TEST_VALUE=from-file\n", encoding="utf-8")

    monkeypatch.setenv("MR_TEST_VALUE", "from-environment")
    load_dotenv(env)
    assert __import__("os").environ["MR_TEST_VALUE"] == "from-environment"


def test_load_dotenv_is_a_noop_when_absent(tmp_path: Path) -> None:
    from marketradar.cli import load_dotenv

    assert load_dotenv(tmp_path / "nope.env") == 0
