from __future__ import annotations

import pytest

from marketradar.cli import EXIT_NOT_IMPLEMENTED, EXIT_OK, build_parser, main

EXPECTED_COMMANDS = {"prices", "screens", "digest", "backfill", "selftest", "manifest"}


def test_every_planned_command_is_registered() -> None:
    parser = build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert actions, "no subcommands registered"
    assert EXPECTED_COMMANDS <= set(actions[0].choices)


# Implemented commands are excluded: `manifest` reads the real manifest,
# `selftest` publishes to R2 and GitHub for real, `migrate` needs Supabase,
# `prices` spends Tiingo quota, and `screens` reads prices from R2. None
# belong in a suite that runs offline. The screen's own logic is covered
# against hand-built prices in tests/test_volatility.py.
IMPLEMENTED = {
    "manifest", "selftest", "migrate", "prices", "screens", "fred", "digest",
    "edgar",
}


@pytest.mark.parametrize("command", sorted(EXPECTED_COMMANDS - IMPLEMENTED))
def test_unimplemented_commands_exit_non_zero(command: str, capsys) -> None:
    """Stubs must fail loudly, not silently do nothing."""
    assert main([command]) == EXIT_NOT_IMPLEMENTED
    assert "not implemented" in capsys.readouterr().err


@pytest.mark.parametrize("command", sorted(IMPLEMENTED))
def test_implemented_commands_are_not_stubs(command: str) -> None:
    """Registered, and routed away from the not-implemented path."""
    import inspect

    from marketradar import cli

    pending_block = inspect.getsource(cli.main).split("pending = ")[1]
    assert command not in pending_block


def test_prices_checks_credentials_before_touching_the_network(monkeypatch) -> None:
    """A missing token must fail before the multi-megabyte universe download.

    This test previously reached the network to discover the token was
    missing, which is exactly the ordering bug it now guards.
    """
    from marketradar.sources import tiingo

    monkeypatch.delenv(tiingo.ENV_TOKEN, raising=False)

    def explode(*a, **k):
        raise AssertionError("fetch_universe called before the token check")

    monkeypatch.setattr(tiingo, "fetch_universe", explode)
    assert main(["prices"]) == 1


def test_bare_invocation_prints_help(capsys) -> None:
    assert main([]) == EXIT_OK
    assert "usage: mr" in capsys.readouterr().out


def test_manifest_command_works_offline(local_manifest, capsys) -> None:
    assert main(["manifest"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "prices_eod_raw/2026" in out
    assert "engine:" in out


def test_date_argument_is_parsed_to_a_date_object() -> None:
    from datetime import date

    args = build_parser().parse_args(["prices", "--date", "2026-09-04"])
    assert args.date == date(2026, 9, 4)


def test_bad_date_is_rejected() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["prices", "--date", "09/04/2026"])


def test_prices_has_the_restart_flag() -> None:
    """Resume is the default; restarting a 12k sweep must be explicit."""
    assert build_parser().parse_args(["prices"]).restart is False
    assert build_parser().parse_args(["prices", "--restart"]).restart is True


def test_selftest_has_the_staleness_injector() -> None:
    args = build_parser().parse_args(["selftest", "--inject-staleness"])
    assert args.inject_staleness is True
