"""Rules about the repo itself, enforced as tests.

Both checks pass trivially while ``sources/`` is empty. That is the point —
an empty-passing test now beats a retrofitted test later, because it starts
failing the moment the first source module is written incorrectly.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

SRC = Path(__file__).resolve().parents[1] / "src" / "marketradar"
SOURCES = SRC / "sources"
SCREENS = SRC / "screens"


def _source_modules() -> list[Path]:
    """Every real loader file, **including inside a source package**.

    ``rglob``, not ``glob``. A source big enough to be a package -- xbrl is
    three files: fetch, tag map, loader -- was invisible to all three rules
    below while this was non-recursive: no URL check, no freshness check, no
    ban on non-deterministic row picks. Nothing failed, which is the problem.
    A rule that silently stops applying to the largest module in the tree is
    worse than no rule, because the tree looks covered.
    """
    return sorted(p for p in SOURCES.rglob("*.py") if p.name != "__init__.py")


def _screen_modules() -> list[Path]:
    """Every screen. They do not load data, but they do shape what is read."""
    return sorted(p for p in SCREENS.rglob("*.py") if p.name != "__init__.py")


def _source_units() -> list[tuple[str, list[Path]]]:
    """One entry per *source*, which is a module or a package of them.

    The freshness rule is per source, not per file: a package's loader calls
    ``assert_fresh`` and its fetch and tag-map halves have nothing to assert
    about. Demanding the call in every file would either force a meaningless
    call into a pure mapping table or -- much more likely -- get the rule
    deleted. So the unit is the thing that loads, and the test below asks only
    that *something* in it ends with the assertion.
    """
    units: list[tuple[str, list[Path]]] = []
    for path in sorted(SOURCES.iterdir()):
        if path.is_dir() and (path / "__init__.py").exists():
            files = [p for p in sorted(path.rglob("*.py"))
                     if p.name != "__init__.py"]
            if files:
                units.append((f"{path.name}/", files))
        elif path.suffix == ".py" and path.name != "__init__.py":
            units.append((path.name, [path]))
    return units


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
    def calls_assert_fresh(module: Path) -> bool:
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        return any(
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

    units = _source_units()
    assert units, "the invariant is vacuous; no sources were found"
    delinquent = [name for name, files in units
                  if not any(calls_assert_fresh(f) for f in files)]

    assert not delinquent, (
        f"These modules never call assert_fresh(): {', '.join(delinquent)}. "
        "Every pipeline stage ends with a freshness assertion — a job that "
        "exits green on empty data is the failure mode this project cares "
        "most about."
    )


#: SQL that picks one row from a group without saying which one. Every one of
#: these is a legitimate function and every one of them makes the output of a
#: job depend on the order DuckDB happened to scan in.
NONDETERMINISTIC_PICKS: Final[tuple[str, ...]] = (
    "any_value", "arbitrary", "first", "last", "reservoir_sample", "random",
)


def _sql_strings(tree: ast.AST) -> list[str]:
    """String literals with the SQL comments stripped out.

    SQL lives in string literals here, and so does the prose explaining why a
    given function is banned -- ``-- min(), not any_value()`` sits inside the
    query it is describing. Scanning the raw string would make the comment
    that documents the rule the thing that fails it.
    """
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            body = re.sub(r"--[^\n]*", "", node.value)
            body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
            out.append(body)
    return out


def test_no_nondeterministic_row_picks_in_pipeline_sql() -> None:
    """A job run twice on identical input must produce identical output.

    Not "must not write duplicates" -- that is idempotency, and this codebase
    already had it when the bug landed. ``build_sponsors`` picked the sponsor
    name with ``any_value()``, and three loads of byte-identical input
    produced 22,680, 22,685 and 22,686 review rows, because which spelling
    won decided whether the sponsor name-matched an SEC filer at all. Every
    write was a correct upsert. Every existing test passed.

    The class of defect is a function that picks a row from a group without
    saying which row, so this bans them from ``sources/`` and ``screens/``.
    ``min``/``max``/``min_by``/``max_by`` say which row and are the fix: they
    are a rule rather than a coin flip, which is the entire difference.
    """
    offenders: list[str] = []
    scanned = 0
    for module in _source_modules() + _screen_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        scanned += 1
        for sql in _sql_strings(tree):
            for fn in NONDETERMINISTIC_PICKS:
                if re.search(rf"\b{fn}\s*\(", sql, flags=re.I):
                    offenders.append(f"  {module.name}: {fn}(")

    assert scanned, "the invariant is vacuous; no modules were scanned"
    assert not offenders, (
        "Non-deterministic row picks in pipeline SQL:\n"
        + "\n".join(sorted(set(offenders)))
        + "\n\nThese choose a row from a group without saying which one, so "
        "the same input gives a different answer per run. Use min()/max() or "
        "min_by()/max_by() on an explicit key instead. Jobs are idempotent "
        "(CLAUDE.md), and idempotent writes of drifting values still drift."
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


#: A ``COPY ... TO '{var}'`` target, so the variable holding the path can be
#: followed. Textual rather than AST: the target is inside an f-string, and the
#: placeholder is the part that matters.
_COPY_TARGET: Final[re.Pattern[str]] = re.compile(
    r"COPY\s*\(?.*?TO\s*'\{([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE | re.DOTALL
)


def _functions(module: Path) -> list[tuple[str, str]]:
    """``(qualified name, source text)`` for every function in a module."""
    text = module.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(module))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((f"{module.name}:{node.name}", ast.get_source_segment(
                text, node) or ""))
    return out


def test_a_file_written_and_read_back_is_never_a_shared_path() -> None:
    """The rule that would have caught ``tiingo.publish``.

    It merged each year partition into a fixed ``staging/merged.parquet`` and
    read the result back from that path, so on a fast POSIX runner every
    partition after the first published and asserted the *previous* year's
    rows. 2024 got 2023. It passed on Windows for weeks and failed the first
    time the suite ran on an Actions runner.

    The defect is not the stale read, which is a platform detail. It is that a
    path written and then read back inside one function was **shared between
    calls** -- so whether the read saw this call's bytes depended on timing.
    Two ways to not share it, and a function doing this has to use one:

    * a per-call scratch directory (``TemporaryDirectory``, ``mkstemp``), which
      is what ``selftest`` and ``sec_company_tickers`` already did, or
    * a filename that interpolates the thing the caller varies -- the plan
      year, the quarter -- so two calls cannot collide. ``form5500.publish``
      and ``xbrl.resolve.load`` both qualify this way.

    Checked on the function's source text because the path is built inside an
    f-string and the placeholder is the whole point. A false positive here is a
    function told to name its scratch file properly, which is cheap; a false
    negative is a partition holding the wrong year.
    """
    offenders: list[str] = []
    scanned = 0
    for module in sorted(SRC.rglob("*.py")):
        for name, body in _functions(module):
            target = _COPY_TARGET.search(body)
            if not target:
                continue
            var = target.group(1)
            # Only the write-then-read-back shape. A function that writes and
            # walks away cannot read anything stale.
            reads_back = re.search(
                rf"read_(?:parquet|csv)\(\s*'?\{{?{re.escape(var)}\b", body)
            if not reads_back:
                continue
            scanned += 1
            per_call = ("TemporaryDirectory" in body or "mkstemp" in body)
            # `x = ... f"...{something}..."` -- the filename varies per call.
            #
            # Line by line, deliberately. The first version ran this over
            # the whole function with DOTALL, so the assignment matched an
            # f-string hundreds of lines later and the rule passed on the
            # very code it was written to catch. Found by reverting
            # tiingo.publish and watching this test not fail -- which is
            # the only way to know whether an invariant works.
            varies = any(
                re.search(rf"\b{re.escape(var)}\s*=.*?f[\"'][^\"']*\{{", line)
                for line in body.splitlines()
            )
            if not (per_call or varies):
                offenders.append(f"  {name}: writes and reads back {var!r}")

    assert scanned, (
        "the invariant is vacuous; no write-then-read-back was found. Either "
        "the COPY pattern changed or this test stopped matching it."
    )
    assert not offenders, (
        "A path is written and read back inside one function without being "
        "unique per call:\n"
        + "\n".join(offenders)
        + "\n\nWhether the read sees this call's bytes then depends on timing. "
        "Use a per-call scratch directory, or put the partition in the "
        "filename. See tiingo.publish, which published 2023's rows into the "
        "2024 partition this way."
    )
