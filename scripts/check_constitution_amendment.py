#!/usr/bin/env python3
"""Fail a pull request that amends the constitution without the required record.

Thin I/O around ``server.sdlc.constitution_gate.check_amendment``: resolve the
base and head versions of ``.specify/memory/constitution.md``, read the PR body,
and exit non-zero with every violation listed.

The file is unchanged in the vast majority of PRs, in which case this exits 0
immediately.

Usage:
    python3 scripts/check_constitution_amendment.py --base-ref origin/master

The PR description is read from ``$PR_BODY`` (set it from
``github.event.pull_request.body`` via ``env:``, never by string interpolation
into the run command).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from server.sdlc.constitution_gate import check_amendment  # noqa: E402

_CONSTITUTION = ".specify/memory/constitution.md"


def _git_show(ref: str, path: str) -> str | None:
    """File contents at *ref*, or None when it does not exist there."""
    proc = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True, text=True, cwd=str(_REPO_ROOT),
    )
    return proc.stdout if proc.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", default="origin/master")
    parser.add_argument("--path", default=_CONSTITUTION)
    args = parser.parse_args(argv)

    head_file = _REPO_ROOT / args.path
    if not head_file.exists():
        print(f"{args.path} does not exist at HEAD — refusing to pass silently")
        return 1
    head_text = head_file.read_text()

    base_text = _git_show(args.base_ref, args.path)
    if base_text is None:
        print(f"{args.path} is new on this branch; treating as an amendment")
        base_text = ""
    elif base_text == head_text:
        print(f"{args.path} unchanged vs {args.base_ref} — nothing to check")
        return 0

    violations = check_amendment(base_text, head_text, os.environ.get("PR_BODY", ""))
    if not violations:
        print(f"constitution amendment OK ({args.path} changed, all checks passed)")
        return 0

    print(f"constitution amendment gate FAILED — {len(violations)} problem(s):\n")
    for violation in violations:
        print(f"  - {violation}")
    print(
        "\nAn amendment must bump the version, record the transition in the Sync "
        "Impact Report, move Last Amended, and carry an owner acknowledgment in the "
        "pull request description (constitution Governance)."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
