"""Rules about the repo itself, enforced as tests.

Both checks pass trivially while ``sources/`` is empty. That is the point —
an empty-passing test now beats a retrofitted test later, because it starts
failing the moment the first source module is written incorrectly.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "marketradar"
SOURCES = SRC / "sources"


def _source_modules() -> list[Path]:
    """Every real loader module. ``__init__.py`` is package scaffolding."""
    return sorted(p for p in SOURCES.glob("*.py") if p.name != "__init__.py")


def test_sources_package_exists() -> None:
    """Guards the two tests below from silently passing on a typo'd path."""
    assert SOURCES.is_dir(), f"{SOURCES} is missing; the invariant tests are vacuous"


def test_no_hardcoded_urls_in_sources() -> None:
    """No literal data URLs in ``sources/``. Everything goes through the manifest.

    Deliberately strict: this flags URLs in comments and docstrings too.
    Reference links belong in ``docs/``, not in loader source, and the
    strictness removes any argument about which literals are "really" data.

    Written as a loop rather than a parametrize so it *passes* rather than
    *skips* while ``sources/`` is empty.
    """
    offenders: list[str] = []
    for module in _source_modules():
        for n, line in enumerate(module.read_text(encoding="utf-8").splitlines(), 1):
            if "http://" in line or "https://" in line:
                offenders.append(f"  {module.name}:{n}: {line.strip()}")

    assert not offenders, (
        "Literal URLs found in sources/:\n"
        + "\n".join(offenders)
        + "\n\nResolve locations through manifest.get(dataset, partition) instead. "
        "Reference links belong in docs/."
    )


def test_every_source_asserts_freshness() -> None:
    """Every loader must call ``assert_fresh``.

    Parsed from the AST rather than grepped, so a commented-out or
    string-mentioned call does not count as adoption. This is the enforcement
    a base class would have given us, without coupling every source to a
    shared parent.
    """
    delinquent: list[str] = []
    for module in _source_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        called = any(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "assert_fresh")
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "assert_fresh"
                )
            )
            for node in ast.walk(tree)
        )
        if not called:
            delinquent.append(module.name)

    assert not delinquent, (
        f"These modules never call assert_fresh(): {', '.join(delinquent)}. "
        "Every pipeline stage ends with a freshness assertion — a job that "
        "exits green on empty data is the failure mode this project cares "
        "most about."
    )


def test_env_example_is_committed_and_dummy() -> None:
    """.env.example exists, and holds nothing that looks real."""
    example = SRC.parents[1] / ".env.example"
    assert example.is_file(), ".env.example must be committed"

    text = example.read_text(encoding="utf-8")
    assert "MR_TIINGO_API_KEY" in text
    assert "MR_R2_SECRET_ACCESS_KEY" in text

    # Only secret-bearing keys have to be placeholders. A bucket name or a
    # hostname is configuration, not a credential.
    secretish = ("KEY", "SECRET", "TOKEN", "PASSWORD", "DSN")
    for line in text.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        if not any(marker in name.upper() for marker in secretish):
            continue
        assert not value or "dummy" in value.lower() or "example" in value.lower(), (
            f".env.example holds a non-placeholder value for {name}: {line}"
        )


def test_gitignore_covers_secrets_and_data() -> None:
    path = SRC.parents[1] / ".gitignore"
    assert path.is_file(), ".gitignore must exist before the first commit"
    body = path.read_text(encoding="utf-8")
    for pattern in (".env", "*.zip", "*.parquet", "__pycache__/", ".venv/"):
        assert pattern in body, f".gitignore is missing {pattern!r}"
    assert "!.env.example" in body, ".env.example must stay committed"
