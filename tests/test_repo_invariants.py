"""Rules about the repo itself, enforced as tests.

Both checks pass trivially while ``sources/`` is empty. That is the point —
an empty-passing test now beats a retrofitted test later, because it starts
failing the moment the first source module is written incorrectly.
"""

from __future__ import annotations

import ast
import fnmatch
import re
from pathlib import Path, PurePosixPath
from typing import Final

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "marketradar"
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


def test_no_upsert_goes_through_duckdbs_postgres_insert_path() -> None:
    """**An ON CONFLICT that DuckDB sends to Postgres silently becomes a COPY.**

    Measured 2026-09-12, by isolating it against `dataset_stats`:

        executemany, no ON CONFLICT      OK
        single execute, no ON CONFLICT   OK
        executemany, WITH ON CONFLICT    FAILED -- COPY, id null
        single execute, WITH ON CONFLICT FAILED -- COPY, id null

    The extension does not implement ON CONFLICT, so it falls back to a plain COPY of
    *every* column -- discarding the column list and every default. On a table with
    `id bigserial primary key` that fails loudly, which is the lucky case: it is how
    this was found, after `mr proxy` reported "3 documents located" and wrote **zero
    rows** three times, and `proxy_section` and `proxy_projection` turned out never to
    have written anything at all.

    The unlucky case is a table whose columns are all nullable with no serial key.
    There the COPY *succeeds* and the upsert silently becomes an append -- duplicate
    rows where an update was intended, and nothing anywhere to say so.

    **Structural, not a line window, and that is the second thing this guard has had
    to learn.** The first version searched the window for the bare word
    `postgres_execute`, and the comment in `sec_trading_symbols.load` explaining why
    it uses `postgres_execute` satisfied the check -- so renaming the actual call left
    the test green. Tightened to require the call shape, it then fired on correct
    code: extracting the statement into `upsert_statement()` put the SQL more than
    sixty lines from the call that sends it. A window assumes a statement sits near
    its call, which stops being true the moment a statement gets a name -- and naming
    it was right, because it is what let a test read the SQL instead of the source
    text.

    So the rule is now what it always meant. SQL containing ON CONFLICT must *reach*
    `postgres_execute`: either the function holding it calls that directly, or it is
    a builder whose return value every caller passes on. Widening the window instead
    would have bought a pass today and hidden the next real one.
    """

    def mentions_postgres_execute(node: ast.AST) -> bool:
        """A `postgres_execute` **call**, not the words. The documentation that
        explains why the call exists must not be able to satisfy the check."""
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            for arg in sub.args:
                # f-strings included: every real call interpolates the alias, so
                # checking only ast.Constant misses all of them. The literal parts
                # are what carry the call shape.
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    parts = [arg.value]
                elif isinstance(arg, ast.JoinedStr):
                    parts = [v.value for v in arg.values
                             if isinstance(v, ast.Constant)
                             and isinstance(v.value, str)]
                else:
                    continue
                # The call shape, not the bare word -- prose explaining why the
                # call uses postgres_execute must not satisfy the check.
                if any("postgres_execute('" in t or "postgres_execute({" in t
                       for t in parts):
                    return True
            if isinstance(sub.func, ast.Attribute) and \
                    sub.func.attr == "postgres_execute":
                return True
        return False

    def holds_on_conflict(node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                    and "on conflict" in sub.value.lower():
                return True
        return False

    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:                                   # not ours to judge
            continue
        funcs = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        # Builders: they hold the SQL but send nothing themselves. Their callers are
        # where the obligation lands.
        builders = {f.name for f in funcs
                    if holds_on_conflict(f) and not mentions_postgres_execute(f)}
        for func in funcs:
            if not holds_on_conflict(func):
                continue
            if mentions_postgres_execute(func):
                continue
            if func.name in builders:
                # A builder is fine in itself; every *caller* must send it through.
                users = [
                    other for other in funcs
                    if other is not func and any(
                        isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                        and c.func.id == func.name
                        for c in ast.walk(other))
                ]
                if not users:
                    offenders.append(
                        f"{path.relative_to(SRC.parent.parent)}:{func.lineno}: "
                        f"{func.name}() builds an upsert nothing sends")
                for user in users:
                    if not mentions_postgres_execute(user):
                        offenders.append(
                            f"{path.relative_to(SRC.parent.parent)}:{user.lineno}: "
                            f"{user.name}() uses {func.name}() without "
                            "postgres_execute")
                continue
            # A local DuckDB table is fine -- the extension is not involved.
            body = ast.get_source_segment(
                path.read_text(encoding="utf-8"), func) or ""
            if any(t in body for t in ("create temp table", "_comps_", "_dcf_")):
                continue
            offenders.append(
                f"{path.relative_to(SRC.parent.parent)}:{func.lineno}: "
                f"{func.name}() holds an upsert and does not send it through "
                "postgres_execute")

    assert not offenders, (
        "an upsert is going through DuckDB's Postgres insert path, which turns "
        "ON CONFLICT into a COPY of every column:\n  "
        + "\n  ".join(offenders)
        + "\n\nSend it through `CALL postgres_execute('pg', ?)` instead. On a table "
        "with a serial key this fails loudly; on one without, the upsert silently "
        "becomes an append."
    )


def test_every_postgres_write_is_counted_not_assumed() -> None:
    """The other half of the same lesson.

    `upsert_corporate_actions` returned `len(rows)` regardless of outcome and
    reported complete success having written a tenth of what it attempted. So a
    write path returns what landed, which means it has to read the table back --
    and the way to check that cheaply is that every writer in `sources/` either
    returns a count it measured or asserts freshness on the table afterwards.
    """
    missing: list[str] = []
    for path in sorted((SRC / "sources").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "postgres_execute" not in text:
            continue
        if "assert_fresh" in text or "anti join" in text.lower():
            continue
        missing.append(str(path.relative_to(SRC.parent.parent)))
    assert not missing, (
        "these modules write to Postgres without asserting what landed:\n  "
        + "\n  ".join(missing)
        + "\n\nA writer that reports what it attempted is how corporate_actions "
        "held 365 splits while staging held 3,724."
    )


#: A ``cik_sql`` interpolation, collapsed to this token before the SQL is scanned.
#: Any other interpolation becomes ``?`` -- the scan cares only whether a CIK
#: reference went through the helper.
CIK_TOKEN: Final[str] = "CIKSQL"

#: An ON clause: everything between ``on`` and the next clause keyword.
_ON_CLAUSE: Final[re.Pattern[str]] = re.compile(
    r"\bon\b(.*?)(?=\b(?:join|left|right|inner|outer|cross|where|group|order|"
    r"having|limit|union|qualify|window)\b|$)", re.I | re.S)
_USING_CIK: Final[re.Pattern[str]] = re.compile(r"\busing\s*\(\s*cik\s*\)", re.I)
_BARE_CIK: Final[re.Pattern[str]] = re.compile(r"\bcik\b", re.I)
#: Only strings that are actually queries. A docstring that happens to contain the
#: words "on" and "cik" is prose, and flagging it would teach the next person to
#: delete the explanation rather than fix the code.
_IS_SQL: Final[re.Pattern[str]] = re.compile(r"\bselect\b.*\bfrom\b", re.I | re.S)


def _cik_scan_targets(path: Path) -> list[tuple[int, str]]:
    """Every SQL-looking string in a module, with cik_sql interpolations marked.

    Docstrings are excluded by position, not by heuristic: the string that opens a
    module, class or function body is documentation whatever it contains.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))

    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in docstrings:
            continue
        if isinstance(node, ast.Constant):
            text = node.value if isinstance(node.value, str) else None
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    parts.append(part.value)
                elif isinstance(part, ast.FormattedValue):
                    expr = ast.unparse(part.value)
                    parts.append(CIK_TOKEN if "cik_sql(" in expr else "?")
            text = "".join(parts)
        else:
            continue
        if not text or not _IS_SQL.search(text):
            continue
        # SQL comments out first. The same trap the `any_value` ban hit: a note
        # explaining the rule must not be the thing that trips it, and this scan
        # flagged the comment in `filers.py` that says why the join is not a USING.
        stripped = re.sub(r"--[^\n]*", " ", text)
        out.append((node.lineno, re.sub(r"\s+", " ", stripped)))
    return out


def test_no_join_compares_a_raw_cik_column() -> None:
    """**Four silent failures, so the rule is enforced rather than documented.**

    A CIK is the right key -- SEC assigns it and never reuses it -- and it has two
    spellings here: the XBRL partitions carry it unpadded (`7332`) and Postgres
    zero-padded to ten (`0000007332`). Those strings do not compare, and every time
    they have failed to, the result was a believable number rather than an error:

    - the DCF was handed **5,499 real betas and matched none of 6,431 filers**
    - the same mismatch returned **zero** peer betas from a working query
    - and joined `shares` to `companies` for **0 of 3,443** filers
    - a measurement script printed **"0 of 3,744 stopped filers carry a recovered
      ticker"** -- a finding, not an error, about a map that had just been built

    So every CIK comparison in a query goes through `entities.cik.cik_sql`, on both
    sides, with **no exceptions**. `lpad` is idempotent, so wrapping an
    already-consistent pair costs a function call; "wrap it only where the sides
    might differ" costs the judgement that has been wrong four times, and a
    carve-out for locally-consistent tables is a carve-out that gets copied into the
    next cross-store join.

    Three shapes are rejected:

    - an ON clause mentioning a `cik` column that did not go through the helper
    - `USING (cik)`, which cannot wrap its columns and so is the one join shape
      unable to state its own normalisation
    - a column aliased `cik10` not produced by the helper, which would otherwise be
      a way to launder a raw CIK into a name the first rule trusts

    Mutated to confirm it fires: reverting `deal_multiples`'s join to
    `a.cik = b.cik`, restoring `USING (cik)` in `filers`, and aliasing a raw column
    as `cik10` each turn it red. It also found a fifth occurrence that no test had
    seen -- `_deal_events` in `cli.py` read `ltrim(c.cik, '0') = d.cik`, normalised
    on one side and in the opposite direction from everywhere else. That one was
    correct *today* and only by coincidence: both forms return 14,700 rows, because
    companies.cik is padded and deals.cik is not. It was a latent failure waiting
    for a loader to start padding the other side.
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "cik.py":                  # the helper defines the form
            continue
        for lineno, sql in _cik_scan_targets(path):
            where = f"{path.relative_to(SRC.parent.parent)}:{lineno}"
            if _USING_CIK.search(sql):
                offenders.append(
                    f"{where}: USING (cik) cannot normalise its columns; "
                    "use an explicit ON with cik_sql on both sides")
            if " join " in f" {sql.lower()} ":
                for clause in _ON_CLAUSE.findall(sql):
                    if _BARE_CIK.search(clause):
                        offenders.append(
                            f"{where}: ON clause compares a raw cik: "
                            f"{clause.strip()[:70]}")
            for match in re.finditer(r"(\S+)\s+as\s+cik10", sql, re.I):
                if CIK_TOKEN not in match.group(1):
                    offenders.append(
                        f"{where}: cik10 not built by cik_sql: "
                        f"{match.group(0)[:60]}")

    assert not offenders, (
        "a CIK comparison is not going through entities.cik.cik_sql:\n  "
        + "\n  ".join(offenders)
        + "\n\nThe two spellings of a CIK do not compare and the failure is always "
        "a believable number rather than an error. Wrap both sides -- lpad is "
        "idempotent, so there is no case where wrapping is wrong."
    )



#: A rendered artifact that carries vendor data out of the warehouse in a
#: portable file. A deck's price page is thousands of sessions of raw Tiingo
#: OHLCV and its fundamentals page is XBRL, so the file *is* the data.
#: Tracked paths allowed to be a binary artifact, with a size ceiling each.
#: Deliberately **empty**. CLAUDE.md permits "a small, deliberately chosen set of
#: parser test fixtures -- never a bulk archive", so when the first one arrives it
#: is added here by hand: a diff naming the file and its reason, rather than a
#: directory that silently accepts whatever is dropped into it.
VENDOR_ALLOWLIST: Final[dict[str, int]] = {}

#: How much of a file to read when deciding whether it is binary. A NUL in the
#: first 8 KB is what git's own heuristic uses.
_SNIFF: Final[int] = 8192

#: The ceiling on an allowlisted fixture. "Small, deliberately chosen" is a rule
#: in CLAUDE.md and was previously unenforced, which is how "one parser fixture"
#: becomes a bulk archive one commit at a time.
FIXTURE_MAX_BYTES: Final[int] = 256 * 1024


def _tracked_files() -> list[str]:
    """Every path in the index, from git rather than from a walk.

    The index is the thing that gets pushed. A filesystem walk would see the
    27 gitignored decks sitting in `.decks/` and a `.gitignore` grep would see
    only the intention -- neither is the fact this rule is about.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=REPO, capture_output=True,
            text=True, timeout=60, check=True).stdout
    except Exception as exc:                       # no git, or not a checkout
        import pytest

        pytest.skip(f"git is not available to read the index: {exc}")
    return [p for p in out.split("\0") if p]


def _binary_attribute_patterns() -> list[str]:
    """The glob patterns `.gitattributes` itself marks `binary`.

    **Derived rather than listed, and that is the whole repair.** The rule this
    replaces carried its own tuple of extensions, which is the same mistake one
    level up: a hand-kept list of artifact types is a list that the next artifact
    type is missing from. `.gitattributes` already enumerates every binary shape
    this repo expects to see -- it is the file that said `*.pptx binary` while six
    decks sat in the tree -- so it is the list, and adding a type there now arms
    this check for free.
    """
    path = REPO / ".gitattributes"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) > 1 and "binary" in parts[1:]:
            out.append(parts[0])
    return out


def test_no_tracked_file_has_the_shape_of_a_vendor_artifact() -> None:
    """**Six decks were committed to a public repo, and no rule was asking.**

    Found 2026-09-13 while automating deck generation. `3083eb8` added
    `.decks/*.pptx` and `.decks/before/*.pptx`; each one carries a price page
    built from 2,688 sessions of raw Tiingo OHLCV and a fundamentals page from
    XBRL, so the file *is* the data. That is the boundary the R2 bucket exists
    for and the reason `.dashboard/` is ignored, and a public repo's contents are
    downloadable, which makes a committed deck redistribution under terms that
    forbid it.

    **Why every existing rule missed it, because that is the part that generalises.**
    `test_gitignore_covers_secrets_and_data` checks that *patterns are present* --
    `*.parquet`, `*.zip`, `.dashboard/` all were, and a deck is none of them. And
    `.gitattributes` carried a `*.pptx binary` line, added so a force-added deck
    would not be line-ending mangled. The repo had already identified the file
    type; it had thought about the *encoding* and not about the exposure.

    So the question here is neither "is the pattern listed" nor "is this
    extension on my list". It is **does any tracked file have the shape of an
    artifact**, answered two ways that fail independently:

    - its path matches a glob `.gitattributes` marks `binary` -- the repo's own
      enumeration, so a future `*.xlsx binary` line arms this check with no edit
      here
    - its bytes contain a NUL in the first 8 KB -- a binary file however it is
      named, which catches the parquet called `notes.txt` that no extension list
      can

    `git ls-files`, never a filesystem walk: 27 gitignored decks are sitting in
    `.decks/` as this runs and none of them is the index.

    Mutated to confirm it fires, three ways: run before `git rm --cached` it
    named all six decks; deleting the `*.pptx binary` line from `.gitattributes`
    leaves the NUL sniff catching them anyway; and a text file with a NUL byte
    committed under a `.py` name is caught by the sniff with no attribute at all.
    """
    tracked = _tracked_files()
    assert tracked, "the invariant is vacuous; git listed no tracked files"
    patterns = _binary_attribute_patterns()
    assert patterns, (
        ".gitattributes declares no binary types, so half this check is "
        "vacuous. It listed *.parquet, *.zip and *.pptx when this was written.")

    offenders: dict[str, str] = {}
    for rel in tracked:
        allowed = VENDOR_ALLOWLIST.get(rel)
        path = REPO / rel
        for pattern in patterns:
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(
                    PurePosixPath(rel).name, pattern):
                offenders[rel] = f"matches `{pattern} binary` in .gitattributes"
                break
        if rel not in offenders and path.is_file():
            try:
                head = path.read_bytes()[:_SNIFF]
            except OSError:                        # unreadable is not a pass
                offenders[rel] = "could not be read to check whether it is binary"
                continue
            if b"\0" in head:
                offenders[rel] = "contains a NUL byte, so it is a binary file"
        if rel in offenders and allowed is not None:
            size = path.stat().st_size if path.is_file() else 0
            if size <= allowed:
                del offenders[rel]
            else:
                offenders[rel] = (
                    f"is allowlisted at {allowed:,} bytes but is {size:,}; "
                    '"small, deliberately chosen" is the rule and this is a '
                    "bulk archive")

    assert not offenders, (
        "these tracked files have the shape of a vendor artifact:\n  "
        + "\n  ".join(f"{p}: {why}" for p, why in sorted(offenders.items()))
        + "\n\nThe repo holds code and SQL. A deck, a parquet or a workbook "
        "carries Tiingo prices or XBRL in a portable file, and this repo is "
        "public, so committing one redistributes vendor data to anyone who can "
        "clone it. Untrack it (`git rm --cached`) and let .gitignore hold it; "
        "vendor data goes to R2.\n\nIf the file genuinely is not vendor-derived "
        "-- a parser fixture -- add it to VENDOR_ALLOWLIST with a size ceiling, "
        "so that it is a diff somebody reviewed rather than a directory that "
        "accepts anything."
    )


def test_an_allowlisted_fixture_is_capped_rather_than_trusted() -> None:
    """"Small, deliberately chosen" is a rule, so it has a number.

    The allowlist is empty today, which makes this vacuous -- the same way the
    source rules passed trivially while `sources/` was empty, and for the same
    reason: an empty-passing rule now beats a retrofitted one later. What it
    guards is the drift where one 4 KB parser fixture becomes a 40 MB archive
    across six commits nobody read together.
    """
    assert FIXTURE_MAX_BYTES <= 1024 * 1024, (
        "the fixture ceiling has grown past a megabyte, which is no longer "
        "'small'. A bulk archive belongs in R2 or a Release, per CLAUDE.md.")
    for rel, ceiling in VENDOR_ALLOWLIST.items():
        assert ceiling <= FIXTURE_MAX_BYTES, (
            f"{rel} is allowlisted at {ceiling:,} bytes, above the "
            f"{FIXTURE_MAX_BYTES:,} ceiling")
        assert (REPO / rel).is_file(), (
            f"{rel} is allowlisted but is not in the tree. A stale allowlist "
            "entry is a hole waiting for a file with that name.")


#: `cik_sql(column) = ?` -- the helper on one side of a comparison and a bind
#: parameter or a plain interpolation on the other. In the scanned form every
#: non-`cik_sql` interpolation has already collapsed to `?`, so this one pattern
#: catches a literal bind marker and an unwrapped f-string alike.
_HALF_WRAPPED_CIK: Final[re.Pattern[str]] = re.compile(
    rf"{CIK_TOKEN}\s*(?:=|<>|!=)\s*\?|\?\s*(?:=|<>|!=)\s*{CIK_TOKEN}")


def test_no_cik_comparison_wraps_only_its_column() -> None:
    """**The sixth occurrence, and the first one the ON-clause rule could not
    see.**

    `_deck_subject` read ``where lpad(cast(cik as varchar), 10, '0') = ?`` and
    passed a CIK the DCF rows carry unpadded. The column went through the helper
    and the *parameter* did not, so the two strings never compared: measured
    2026-09-13 directly against the partitions, 0 rows for `'66740'` and 56 for
    `'0000007332'`-style padding.

    Nothing raised, and the damage was invisible in the place it landed. Every
    deck ever rendered came out with a blank fundamentals page, a blank cash-flow
    page, no SIC and a filing window of "? to ?" -- and the cash-flow page went
    further than blank, printing "Absent capex is not zero capex ... this free
    cash flow is an upper bound" for 3M, which reports $910M of capex. A caveat
    fired by a failed join reads exactly like a fact about the company.

    `test_no_join_compares_a_raw_cik_column` could not catch it: there is no join
    here, and the column *did* go through `cik_sql`. Half-wrapped is its own
    shape, so it gets its own rule. `cik_sql('?')` is the fix and was already the
    idiom two queries away in the same function.

    Mutated to confirm it fires: reverting either of the two `_deck_subject`
    reads to `= ?` turns it red and names the line.
    """
    offenders: list[str] = []
    scanned = 0
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "cik.py":                  # the helper defines the form
            continue
        for lineno, sql in _cik_scan_targets(path):
            scanned += 1
            if _HALF_WRAPPED_CIK.search(sql):
                offenders.append(
                    f"{path.relative_to(SRC.parent.parent)}:{lineno}: "
                    "cik_sql on the column and a bare parameter on the other "
                    "side")

    assert scanned, "the invariant is vacuous; no SQL was scanned"
    assert not offenders, (
        "a CIK comparison wraps its column and not its parameter:\n  "
        + "\n  ".join(offenders)
        + "\n\nThe padded and unpadded spellings do not compare, and this shape "
        "fails silently in the direction that looks like data: a blank page, or "
        "worse, a caveat about missing capex on a filer that reports it. Wrap "
        "both sides -- `cik_sql('?')` is idempotent and already the idiom."
    )



#: Every hand-rolled spelling of a CIK normaliser. Found by grepping for the
#: *operation* rather than for the word "cik", which is how six of them had
#: survived a rule whose docstring said there must be one.
_HAND_ROLLED_CIK: Final[tuple[tuple[str, str], ...]] = (
    (r"lstrip\(\s*['\"]0['\"]\s*\)", "lstrip('0')"),
    (r"\.zfill\(\s*10\s*\)", ".zfill(10)"),
    (r"\.rjust\(\s*10", ".rjust(10, '0')"),
    (r":010d", 'f"{...:010d}"'),
    # **Width ten, not `lpad(` on its own.** The first version of this list
    # flagged `selftest.py`'s `lpad((i % 250)::VARCHAR, 4, '0')`, which pads a
    # synthetic *ticker* to four and has nothing to do with a CIK.
    #
    # Zero-padding to ten is only ever a CIK here -- an accession is eighteen
    # characters and a ticker is five -- so the width is what makes the pattern
    # specific. Narrowing it to "lines that mention cik" was the other option and
    # it is worse: it would miss a helper named `_pad`, which is exactly the shape
    # the next hand-rolled normaliser takes once the obvious ones are gone.
    (r"lpad\s*\([^;]*?,\s*10\s*,", "lpad(x, 10) instead of cik_sql"),
    (r"ltrim\s*\([^;]*?,\s*['\"]0['\"]", "ltrim(x, '0') instead of cik_sql"),
)


def test_cik_key_is_the_only_way_to_normalise_a_cik() -> None:
    """**The seventh occurrence, and the one that says the rule was unfinished.**

    `entities/cik.py` existed, its docstring said "a convention is not a rule, it
    is a hope", and a repo rule enforced the helper at every *join*. Measured
    2026-09-13 by grepping for the operation instead of for the word: **fifteen
    hand-rolled normalisers across five modules, in six different spellings.**

    - `sources/sec_trading_symbols.py` held
      `str(cik).strip().lstrip("0").rjust(10, "0")` -- `cik_key`'s body, copied
    - `signals/deals.py` produced two *different* forms four lines apart,
      `str(cik).lstrip("0").zfill(10)` for one field and `str(cik).lstrip("0")`
      for another
    - `screens/comps.py` had a sixth, `str(k).strip().lstrip("0")`, with a comment
      recording the failure it had already caused: 0 peer betas from 5,499 real
      ones
    - and `sources/sec_trading_symbols.py` also had `f"{int(cik):010d}"`, which
      additionally raised on any CIK that was not already an integer

    None of them was wrong on the day it was written, which is the whole problem:
    each was correct for its own call site and none was a rule, so the next one
    was written from scratch too. The join rule could not see any of them --
    there is no join in `f"{int(cik):010d}"`.

    So the answer to "can the parameter side be caught" is that catching
    parameters is not enough: **the helper has to be the only thing in the
    codebase that knows how a CIK is spelled.** There are exactly two canonical
    forms, `cik_key` padded and `cik_bare` unpadded, and `cik_sql` for SQL. This
    bans every other way of producing one.

    Mutated to confirm it fires: restoring any one of the fifteen turns it red and
    names the file and line. The `.zfill(10)` in `edgar_rss` and the `.rjust(10)`
    in `sec_trading_symbols` were each reverted and each caught.
    """
    offenders: list[str] = []
    scanned = 0
    for path in sorted(SRC.rglob("*.py")):
        if path.relative_to(SRC).as_posix() == "entities/cik.py":
            continue                       # the helper defines the spelling
        scanned += 1
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            # Comments are stripped first, for the same reason the `any_value`
            # ban strips SQL comments: the note explaining the rule must not be
            # the thing that trips it. This scan flagged its own explanation in
            # four modules before the strip was added.
            code = line.split("#", 1)[0]
            if not code.strip():
                continue
            for pattern, name in _HAND_ROLLED_CIK:
                if re.search(pattern, code, flags=re.I):
                    offenders.append(
                        f"{path.relative_to(SRC.parent.parent)}:{lineno}: "
                        f"{name} -- {code.strip()[:72]}")

    assert scanned, "the invariant is vacuous; no modules were scanned"
    assert not offenders, (
        "a CIK is being normalised by hand:\n  "
        + "\n  ".join(offenders)
        + "\n\nThere are two canonical spellings and both live in "
        "entities/cik.py: `cik_key` padded to ten, for comparisons, dict keys "
        "and anything stored; `cik_bare` unpadded, for EDGAR's archive paths "
        "and the deals.cik column. `cik_sql` wraps a column or a bind parameter "
        "for SQL.\n\nThis is not style. Fifteen hand-rolled normalisers in six "
        "spellings is how the same defect reaches its seventh occurrence, and "
        "every one of them fails as a believable number rather than an error."
    )


# --- the deal-outcome limitation, enforced rather than documented -------


#: Columns that ARE a deal-outcome number. Rendering one of these obliges the
#: renderer to say what population it is measured on.
_EXCESS_COLUMNS: Final[tuple[str, ...]] = (
    "median_excess", "mean_excess", "win_rate")


def test_only_one_copy_of_the_survivorship_caveat_exists() -> None:
    """**The comment promising this already existed, and was false.**

    `panels.py` held its own copy under the words "One sentence, shared with the CLI
    and the spec so the three cannot drift" -- and the two wordings had already
    drifted: the panel's omitted "by an unknown amount" and never named the
    population. A comment cannot keep two strings in step; the same shape as the two
    CIK normalisers that agreed on every normal input until they did not.

    So the panel imports `outcomes.SURVIVOR_CAVEAT` and this fails the build on a
    second literal. Detected by the distinctive phrase rather than the whole
    sentence, because a near-copy is the failure mode -- an exact duplicate would at
    least stay correct.
    """
    # Long enough to be the caveat rather than a sentence about it. The first
    # version used "delists the target", which matched a docstring in panels.py
    # explaining why the `priced` column drops rows -- an explanation of the
    # problem, not a second copy of the warning.
    markers = ("weighted toward deals", "higher than this by an unknown")
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "outcomes.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        docstrings = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None) or []
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)) and body \
                    and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if id(node) in docstrings:
                continue
            if not (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)):
                continue
            for marker in markers:
                if marker in node.value:
                    offenders.append(
                        f"{path.relative_to(SRC.parent.parent)}:{node.lineno}: "
                        f"{marker!r}")
    assert not offenders, (
        "the survivorship caveat is written out a second time instead of imported "
        "from screens/outcomes.py:\n  " + "\n  ".join(offenders))


def test_a_renderer_of_excess_returns_states_the_population() -> None:
    """Any module that renders a deal-outcome number must reach for the caveat.

    The rule is about *renderers*, which is why it keys on the output columns rather
    than on the word "excess": a module that computes or persists these is not
    showing them to anybody. A deck page or a digest block added later is caught by
    this, which is the point -- neither renders one today, and the limitation has to
    survive the next thing that does.
    """
    # **An explicit output layer, because the indirection defeats a static check.**
    # The panel reads `r["median_excess"]` into a local and interpolates `pct(ex)`,
    # so no f-string mentions the column and an AST scan for interpolations found
    # nothing -- the guard passed panels.py by missing it, not by approving it.
    # Tracking the value needs dataflow analysis, so the rule names the modules that
    # emit an artifact instead.
    #
    # `dashboard/shell.py` is deliberately absent: it selects `median_excess::text`
    # into a dict and formats nothing. The exclusion is one named module with a
    # reason rather than a flag, so a new module does not silently inherit it.
    output_layer = (
        "dashboard/panels.py", "dashboard/detail.py", "decks.py", "digest.py")
    offenders = []
    for rel in output_layer:
        path = SRC / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if not any(col in text for col in _EXCESS_COLUMNS):
            continue
        # The shared constant by name, not a local that merely looks like it. A
        # first version accepted any module containing the substring `_SURVIVOR`,
        # which a local variable holding a hand-written copy satisfies -- so it
        # passed exactly the module that had stopped importing the real thing.
        if "SURVIVOR_CAVEAT" in text or "SURVIVOR_FLAG" in text:
            continue
        offenders.append(rel)
    assert not offenders, (
        "these render a deal-outcome number without the population it is measured "
        f"on: {offenders}. Import outcomes.SURVIVOR_CAVEAT and render it beside the "
        "figure -- the bias runs the same direction as the number, so a reader who "
        "sees only the figure reads survivorship as a fact about deals.")


def test_the_caveat_is_not_hover_only_in_the_panel() -> None:
    """**"Not a footnote" is testable, so it is tested.**

    The panel used to carry the caveat only inside `title=` attributes on two column
    headers. That is a tooltip: invisible unless hovered, absent from a printout, and
    absent from a screenshot -- a footnote wearing a hat. Strips every attribute
    value and requires the sentence to still be there.
    """
    import re

    from marketradar.dashboard import panels
    from marketradar.screens import outcomes

    html = panels.outcomes_html([{
        "study": "8-K deals", "slice": "all", "horizon": 1, "n": 1200,
        "median_ret": 0.011, "median_excess": -0.0236, "mean_excess": -0.02,
        "win_rate": 0.48, "median_run_up": 0.004, "n_suspect": 3,
        "events": 1200, "priced": 800,
    }])
    assert "-2.36%" in html, "the fixture did not render a figure"
    # Attribute values out: what is left is what a reader sees without hovering.
    visible = re.sub(r'\b[a-zA-Z-]+="[^"]*"', " ", html)
    needle = "Measured on acquirers"
    assert needle in visible, (
        "the survivorship caveat renders only inside an attribute (a tooltip), so a "
        "reader sees the number and not the population it is measured on")
