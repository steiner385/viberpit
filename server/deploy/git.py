from __future__ import annotations
import logging
import subprocess
import time
from typing import Callable

from server.deploy.steps import err_reason
from server.github.remote_url import slug_from_remote_url

log = logging.getLogger("server.deploy.git")

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


def origin_slug(repo: str, run: Run = subprocess.run) -> str:
    """``owner/name`` of the checkout's ``origin`` remote. Raises when there is no
    origin or its URL is not a recognisable GitHub remote."""
    return slug_from_remote_url(_out(repo, "config", "--get", "remote.origin.url", run=run))


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


def fetch(repo: str, ref: str = "master", run: Run = subprocess.run, *,
          attempts: int = 3, backoff_s: float = 2.0,
          sleep: Callable[[float], object] = time.sleep) -> None:
    """``git fetch origin <ref>``, retried with doubling backoff on a non-zero exit.

    The deploy checkout fetches over HTTPS and gets its credentials from the
    ``gh auth git-credential`` helper, whose token lives in the desktop keyring. When
    that keyring call times out (a swapped-out daemon under memory pressure — 2026-09-09
    14:55 coincided with an OOM kill, 2026-09-11 06:36 with swap at 97%), gh prints
    nothing, git tries to prompt with no tty and dies with exit 128 ("could not read
    Username"). Each blip cleared within one 30s detect tick, but preflight ran the
    fetch once, so a blip at the moment of "Deploy now" aborted the whole deploy.
    Three attempts over ~6s ride it out; a genuine auth/config break still raises.
    """
    delay = backoff_s
    for attempt in range(1, attempts + 1):
        try:
            run(["git", "-C", repo, "fetch", "--quiet", "origin", ref], check=True,
                capture_output=True, text=True)
            return
        except subprocess.CalledProcessError as e:
            if attempt >= attempts:
                raise
            log.warning("git fetch origin %s in %s failed (attempt %d/%d): %s — "
                        "retrying in %.0fs", ref, repo, attempt, attempts,
                        err_reason(e), delay)
            sleep(delay)
            delay *= 2


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
