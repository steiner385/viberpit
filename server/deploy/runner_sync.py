"""Boot-time version reconcile for a session runner (spec §3).

Wired as ``ExecStartPre=<venv>/bin/python -m server.deploy.runner_sync`` on the daemon
unit. Reads ``deploy_state.deployed_sha`` with RAW SQL — this script runs on the OLD
checkout, which may predate the ORM model — and fast-forwards the checkout to it when
the runner is behind. Only ever moves FORWARD: a HEAD that is a descendant of the
deployed sha means a deploy is mid-flight (the fan-out moves checkouts before it records
the new version) and is left alone.

Every failure is logged to the journal at ERROR and the process exits 0 (decision 3a):
a runner that cannot reconcile still starts and rehydrates its sessions; the fleet
probe's skew gate keeps it out of dispatch until an operator fixes it.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from typing import Callable, Literal

log = logging.getLogger("ccswitch.runner_sync")

Run = Callable[..., subprocess.CompletedProcess]
Outcome = Literal["no-state", "in-sync", "ahead", "updated", "dirty", "not-fast-forward",
                  "fetch-failed", "checkout-failed", "pip-failed", "unknown-sha"]

FETCH_TIMEOUT_S = 60
DEPS_FILES = ("pyproject.toml", "uv.lock")


def read_deployed_sha(db_url: str, *, timeout_s: float = 15.0) -> str | None:
    """``deploy_state.deployed_sha`` via the driver directly (asyncpg / sqlite3).
    None when there is no row, no table, or the DB is unreachable within *timeout_s*."""
    sql = "SELECT deployed_sha FROM deploy_state WHERE id = 'singleton'"
    try:
        if db_url.startswith("sqlite"):
            import sqlite3
            path = db_url.split("///", 1)[1]
            if not os.path.exists(path):
                return None
            with sqlite3.connect(path, timeout=timeout_s) as conn:
                row = conn.execute(sql).fetchone()
            return row[0] if row and row[0] else None
        import asyncpg  # noqa: PLC0415

        async def go():
            conn = await asyncio.wait_for(
                asyncpg.connect(db_url.replace("postgresql+asyncpg://", "postgresql://")),
                timeout=timeout_s)
            try:
                return await asyncio.wait_for(conn.fetchval(sql), timeout=timeout_s)
            finally:
                await conn.close()
        return asyncio.run(go()) or None
    except Exception as exc:  # noqa: BLE001 — unreadable state is "no state", loudly
        log.error("runner_sync: cannot read deploy_state (%s: %s)", type(exc).__name__, exc)
        return None


def _git(run: Run, repo: str, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout)


def sync(repo: str, *, deployed_sha: str | None, run: Run = subprocess.run) -> Outcome:
    if not deployed_sha:
        log.info("runner_sync: no deploy_state yet — nothing to reconcile")
        return "no-state"
    head = (_git(run, repo, "rev-parse", "HEAD").stdout or "").strip()
    if not head:
        log.error("runner_sync: cannot read HEAD of %s", repo)
        return "unknown-sha"
    if head == deployed_sha or head.startswith(deployed_sha) or deployed_sha.startswith(head):
        log.info("runner_sync: %s already on deployed %s", repo, deployed_sha[:7])
        return "in-sync"
    if (_git(run, repo, "status", "--porcelain").stdout or "").strip():
        log.error("runner_sync: %s has uncommitted changes — refusing to move it "
                  "(HEAD=%s deployed=%s)", repo, head[:7], deployed_sha[:7])
        return "dirty"
    if _git(run, repo, "fetch", "--quiet", "origin", timeout=FETCH_TIMEOUT_S).returncode != 0:
        log.error("runner_sync: git fetch failed in %s — starting on %s", repo, head[:7])
        return "fetch-failed"
    if _git(run, repo, "merge-base", "--is-ancestor", deployed_sha, head).returncode == 0:
        log.info("runner_sync: HEAD %s is AHEAD of deployed %s (deploy in flight) — leaving it",
                 head[:7], deployed_sha[:7])
        return "ahead"
    if _git(run, repo, "merge-base", "--is-ancestor", head, deployed_sha).returncode != 0:
        log.error("runner_sync: HEAD %s is not an ancestor of deployed %s — diverged checkout; "
                  "see docs/runbooks/rocky-session-host.md §5 (reset recipe)", head[:7], deployed_sha[:7])
        return "not-fast-forward"
    # Detached or on a branch? `checkout master` moves HEAD to the LOCAL master, which the
    # ff-only merge below then only moves FORWARD — so if local master already carries
    # commits the deployed sha does not, the checkout alone silently rewinds this runner
    # onto them. Refuse BEFORE touching HEAD; the operator's work is not ours to discard.
    branch = (_git(run, repo, "rev-parse", "--abbrev-ref", "HEAD").stdout or "").strip()
    if branch != "master" and \
            _git(run, repo, "merge-base", "--is-ancestor", "master", deployed_sha).returncode != 0:
        log.error("runner_sync: local master has commits not on the deployed sha — refusing to "
                  "switch branches (HEAD=%s on %r, deployed=%s); see "
                  "docs/runbooks/rocky-session-host.md §5 (reset recipe)",
                  head[:7], branch, deployed_sha[:7])
        return "not-fast-forward"
    co = _git(run, repo, "checkout", "-q", "master")
    if co.returncode != 0:
        log.error("runner_sync: git checkout master failed: %s", (co.stderr or "").strip()[:500])
        return "checkout-failed"
    if _git(run, repo, "merge", "--ff-only", deployed_sha).returncode != 0:
        log.error("runner_sync: ff-only merge to %s failed (local commits on master?)", deployed_sha[:7])
        return "not-fast-forward"
    diff = _git(run, repo, "diff", "--name-only", head, deployed_sha, "--", *DEPS_FILES)
    if diff.returncode != 0:
        log.warning("runner_sync: git diff for %s failed (rc=%s) — assuming deps changed, running pip",
                    DEPS_FILES, diff.returncode)
    changed = diff.returncode != 0 or (diff.stdout or "").strip()
    if changed:
        venv_python = os.path.join(repo, ".venv", "bin", "python")
        r = run([venv_python, "-m", "pip", "install", "-e", f"{repo}[server]", "--quiet"],
                capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            log.error("runner_sync: pip install failed after moving to %s: %s",
                      deployed_sha[:7], (r.stderr or "").strip()[:500])
            return "pip-failed"
    run(["sync"], capture_output=True, text=True, timeout=120)
    log.info("runner_sync: %s → %s (was %s)", repo, deployed_sha[:7], head[:7])
    return "updated"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s",
                        stream=sys.stderr)
    p = argparse.ArgumentParser(prog="runner_sync")
    p.add_argument("--repo", default=os.getcwd())
    args = p.parse_args(argv)
    db_url = os.environ.get("CCSWITCH_DB_URL", "")
    if not db_url:
        log.error("runner_sync: CCSWITCH_DB_URL unset — cannot reconcile")
        return 0
    try:
        outcome = sync(args.repo, deployed_sha=read_deployed_sha(db_url))
        log.info("runner_sync: outcome=%s", outcome)
    except Exception:  # noqa: BLE001 — never block the daemon start
        log.exception("runner_sync: unexpected failure — starting the daemon anyway")
    return 0


if __name__ == "__main__":
    sys.exit(main())
