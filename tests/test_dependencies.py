"""Dependencies that are required but never imported by our own code.

The dangerous kind: nothing in ``src/`` has an ``import pytz`` line, so a
missing install shows up as a runtime error from inside DuckDB, on a machine
that has a database attached, for a column the caller may not even have asked
for. Static analysis cannot see it and the offline suite would never reach it.
So it is asserted here instead.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "module, why",
    [
        (
            "pytz",
            "DuckDB's Postgres scanner needs pytz to convert timestamptz. "
            "Without it, reading any timestamptz column raises "
            "ModuleNotFoundError from inside a query -- corporate_actions."
            "ingested_at, dataset_stats.observed_at, and every signals and "
            "job_queue timestamp in Weekend 3.",
        ),
    ],
)
def test_runtime_dependency_is_installed(module: str, why: str) -> None:
    assert importlib.util.find_spec(module) is not None, (
        f"{module} is not installed. {why}"
    )


def test_pytz_is_declared_in_pyproject() -> None:
    """Installed is not enough; it has to survive `uv sync --frozen` in CI."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    dependencies = text.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "pytz" in dependencies, (
        "pytz must be a declared dependency, not merely present in the local "
        ".venv -- the Action installs with --frozen from the lock file."
    )
