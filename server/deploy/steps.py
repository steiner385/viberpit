from __future__ import annotations
import json
import re
import subprocess
from typing import Callable

from server.deploy.types import DeployError

Run = Callable[..., subprocess.CompletedProcess]
_OK_CONCLUSIONS = {"success", "neutral", "skipped"}


def _default_gh_api(path: str) -> str:
    r = subprocess.run(["gh", "api", path], check=True, capture_output=True, text=True)
    return r.stdout


def ci_verdict(sha: str, gh_api: Callable[[str], str] | None = None) -> str:
    gh_api = gh_api or _default_gh_api
    try:
        body = json.loads(gh_api(f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs"))
    except Exception:
        return "unavailable"
    runs = body.get("check_runs") or []
    if not runs:
        return "pending"
    if any(r.get("status") != "completed" for r in runs):
        return "pending"
    if any((r.get("conclusion") or "") not in _OK_CONCLUSIONS for r in runs):
        return "red"
    return "green"


# Edge-only ALLOWLIST — mirrors scripts/auto-deploy.sh exactly. Anything NOT matching
# (incl. shared modules the daemon imports: server/config, server/db, server/audit,
# server/usage, the orchestrator, IPC, agent-SDK glue) forces a daemon restart.
_EDGE_ONLY = re.compile(
    r"^(web/|server/api/|server/app\.py|server/bridge/|server/spa\.py|"
    r"server/tests/|docs/|deploy/|\.github/|.*\.md$)"
)


def classify_change(changed_files: list[str] | None) -> str:
    if changed_files is None:
        return "daemon_changed"   # fail-safe: cannot classify → treat as daemon change
    if all(_EDGE_ONLY.match(f) for f in changed_files):
        return "edge_only"
    return "daemon_changed"


def guard_working_tree(repo: str, run: Run = subprocess.run) -> bool:
    """Guard against accidental commits: if tree is dirty, reset --hard.

    Returns True if dirty (discarded), False if clean.
    Untracked files are untouched (no -x flag on git diff).
    """
    dirty = run(["git", "-C", repo, "diff", "--quiet", "HEAD", "--"]).returncode != 0
    if dirty:
        run(["git", "-C", repo, "reset", "--hard", "HEAD"], check=True, capture_output=True)
    return dirty


def fast_forward(repo: str, ref: str = "master", run: Run = subprocess.run) -> None:
    """Pull with --ff-only from origin.

    Raises DeployError("update", ...) if pull fails (non-zero exit).
    """
    r = run(["git", "-C", repo, "pull", "--ff-only", "origin", ref],
            capture_output=True, text=True)
    if r.returncode != 0:
        raise DeployError("update", f"fast-forward pull failed: {r.stderr.strip()}")
