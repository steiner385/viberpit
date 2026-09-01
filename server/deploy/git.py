from __future__ import annotations
import subprocess
from typing import Callable

Run = Callable[..., subprocess.CompletedProcess]


def _out(repo: str, *args: str, run: Run = subprocess.run) -> str:
    r = run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)
    return r.stdout.strip()


def abs_git_dir(repo: str, run: Run = subprocess.run) -> str:
    return _out(repo, "rev-parse", "--absolute-git-dir", run=run)


def current_sha(repo: str, run: Run = subprocess.run) -> str:
    return _out(repo, "rev-parse", "@", run=run)


def remote_sha(repo: str, ref: str = "origin/master", run: Run = subprocess.run) -> str:
    return _out(repo, "rev-parse", ref, run=run)


def short_sha(repo: str, sha: str, run: Run = subprocess.run) -> str:
    return _out(repo, "rev-parse", "--short", sha, run=run)


def is_ancestor(repo: str, ancestor: str, descendant: str, run: Run = subprocess.run) -> bool:
    """True when *ancestor* is an ancestor of (or equal to) *descendant*.

    Backs the explicit-target preflight gate: a deploy target must sit inside
    ``live..origin/master`` (live is an ancestor of it, and it an ancestor of the tip).
    Any failure — including an unknown rev — is "not an ancestor", never an exception,
    so preflight can refuse cleanly instead of stack-tracing.
    """
    r = run(["git", "-C", repo, "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True, text=True)
    return r.returncode == 0


def fetch(repo: str, ref: str = "master", run: Run = subprocess.run) -> None:
    run(["git", "-C", repo, "fetch", "--quiet", "origin", ref], check=True,
        capture_output=True, text=True)


def log_range(repo: str, a: str, b: str, run: Run = subprocess.run) -> list[dict]:
    fmt = "%H%x1f%s%x1f%an%x1f%cI"
    raw = _out(repo, "log", f"{a}..{b}", f"--format={fmt}", run=run)
    rows: list[dict] = []
    for line in raw.splitlines():
        if not line:
            continue
        sha, subject, author, iso = line.split("\x1f")
        rows.append({"sha": sha, "subject": subject, "author": author, "iso": iso})
    return rows
