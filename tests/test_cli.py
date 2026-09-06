from __future__ import annotations

import pytest

from marketradar.cli import EXIT_NOT_IMPLEMENTED, EXIT_OK, build_parser, main

EXPECTED_COMMANDS = {"prices", "screens", "digest", "backfill", "selftest", "manifest"}


def test_every_planned_command_is_registered() -> None:
    parser = build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert actions, "no subcommands registered"
    assert EXPECTED_COMMANDS <= set(actions[0].choices)


@pytest.mark.parametrize("command", sorted(EXPECTED_COMMANDS - {"manifest"}))
def test_unimplemented_commands_exit_non_zero(command: str, capsys) -> None:
    """Stubs must fail loudly, not silently do nothing."""
    assert main([command]) == EXIT_NOT_IMPLEMENTED
    assert "not implemented" in capsys.readouterr().err


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
