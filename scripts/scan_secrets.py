"""Scan git history for credential values, without ever printing one.

    uv run python scripts/scan_secrets.py                 # scan this repo
    uv run python scripts/scan_secrets.py --clone-from-remote
    uv run python scripts/scan_secrets.py --env path/to/.env

Two rules learned the hard way, both from this tool's own earlier failure:

1. **Classify by value shape, never by variable name.** The first version
   decided what was secret by looking for KEY/SECRET/TOKEN in the variable
   name. ``MR_R2_ACCOUNT_ID`` held a live Cloudflare API token, sailed
   through as "config", and got printed in full. The name can lie; the value
   cannot.

2. **Never print a full value.** Redact to first four and last four
   characters. A scanner that echoes secrets to prove they are secret just
   relocates the leak into terminal scrollback and CI logs.

Exit code is 0 when clean, 1 when any secret-shaped value from the env file
is found in any blob in history.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Ordered most-specific first. Each entry judges a VALUE, never a name.
CRED_SHAPES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^cfat_[A-Za-z0-9_]{20,}$"), "Cloudflare API token"),
    (re.compile(r"^github_pat_[A-Za-z0-9_]{22,}$"), "GitHub fine-grained PAT"),
    (re.compile(r"^(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}$"), "GitHub classic token"),
    (re.compile(r"^AKIA[0-9A-Z]{16}$"), "AWS access key id"),
    (re.compile(r"^re_[A-Za-z0-9_]{15,}$"), "Resend API key"),
    (re.compile(r"^gsk_[A-Za-z0-9]{20,}$"), "Groq API key"),
    (re.compile(r"^csk-[A-Za-z0-9-]{20,}$"), "Cerebras API key"),
    (re.compile(r"^AIza[A-Za-z0-9_-]{30,}$"), "Google AI Studio key"),
    (re.compile(r"^SAM-[0-9a-f-]{30,}$"), "SAM.gov API key"),
    (re.compile(r"^eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"), "JWT / Supabase key"),
    (re.compile(r"://[^:/\s]+:[^@/\s]+@"), "URL with embedded password"),
    (re.compile(r"^[a-f0-9]{64}$"), "64-hex secret"),
    (re.compile(r"^[a-f0-9]{40}$"), "40-hex secret"),
    (re.compile(r"^[a-f0-9]{32}$"), "32-hex secret"),
]

PLACEHOLDER = re.compile(r"dummy|example|placeholder|changeme|your-|localhost", re.I)


def redact(value: str) -> str:
    """First four and last four characters. Never more."""
    if len(value) <= 12:
        return "…" * 3
    return f"{value[:4]}…{value[-4:]}"


def classify(value: str) -> str | None:
    """Name the credential shape, or None if the value is not secret-shaped."""
    for pattern, label in CRED_SHAPES:
        if pattern.search(value):
            return label
    # Fallback: long, unbroken, mixed letters and digits. Catches vendor
    # formats we have not enumerated yet.
    if (
        len(value) >= 20
        and re.fullmatch(r"[A-Za-z0-9_+/=.-]+", value)
        and re.search(r"\d", value)
        and re.search(r"[A-Za-z]", value)
    ):
        return "high-entropy value"
    return None


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip().strip("\"'")
        if value and not PLACEHOLDER.search(value):
            values[name.strip()] = value
    return values


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        args, capture_output=True, text=True, errors="replace", cwd=cwd
    ).stdout


def scan(repo: Path, secrets: dict[str, str]) -> list[tuple[str, str, str]]:
    """Return (variable, label, path) for every secret found in any blob."""
    entries: list[tuple[str, str]] = []
    for line in run("git", "rev-list", "--objects", "--all", cwd=repo).splitlines():
        parts = line.split(maxsplit=1)
        if parts:
            entries.append((parts[0], parts[1] if len(parts) > 1 else "<commit/tree>"))

    findings: list[tuple[str, str, str]] = []
    scanned = 0
    for obj, path in entries:
        if run("git", "cat-file", "-t", obj, cwd=repo).strip() != "blob":
            continue
        scanned += 1
        body = run("git", "cat-file", "-p", obj, cwd=repo)
        for name, value in secrets.items():
            if value in body:
                findings.append((name, classify(value) or "value", path))
    print(f"  blobs scanned: {scanned}")
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", default=".env", type=Path)
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument(
        "--clone-from-remote",
        action="store_true",
        help="clone origin into a temp dir and scan that instead — verifies "
        "what the remote actually serves, not what you think you pushed",
    )
    args = ap.parse_args(argv)

    if not args.env.is_file():
        print(f"no env file at {args.env}; nothing to scan for")
        return 0

    env = load_env(args.env)
    secrets = {n: v for n, v in env.items() if classify(v)}
    benign = {n: v for n, v in env.items() if not classify(v)}

    print(f"env file: {args.env}")
    print(f"  secret-shaped values : {len(secrets)}")
    for name, value in sorted(secrets.items()):
        print(f"      {name:<28} {classify(value):<24} {redact(value)}")
    print(f"  non-secret values    : {len(benign)}")
    for name in sorted(benign):
        print(f"      {name}")
    print()

    with tempfile.TemporaryDirectory() as tmp:
        if args.clone_from_remote:
            url = run("git", "remote", "get-url", "origin", cwd=args.repo).strip()
            if not url:
                print("no origin remote configured")
                return 1
            target = Path(tmp) / "clone"
            print(f"cloning {url}")
            subprocess.run(
                ["git", "clone", "-q", url, str(target)], check=True
            )
            repo = target
        else:
            repo = args.repo
        print(f"scanning {repo}")
        findings = scan(repo, secrets)

    print()
    if findings:
        print("=" * 68)
        print("*** CREDENTIAL FOUND IN HISTORY — ROTATE THESE NOW ***")
        for name, label, path in sorted(set(findings)):
            print(f"    {name:<28} ({label}) in {path}")
        print("=" * 68)
        return 1

    print("=" * 68)
    print(f"CLEAN: 0 of {len(secrets)} secret-shaped values appear in any blob.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
