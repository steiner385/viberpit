from __future__ import annotations
import json
import os
import re
import subprocess
import time
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


# Edge-only ALLOWLIST (fail-safe): only these paths are edge-only; anything NOT matching
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


def changed_paths(repo: str, a: str, b: str, paths: list[str], run: Run = subprocess.run) -> list[str]:
    r = run(["git", "-C", repo, "diff", "--name-only", a, b, "--", *paths],
            capture_output=True, text=True)
    return [p for p in r.stdout.splitlines() if p.strip()]


def sync_deps_if_changed(repo: str, a: str, b: str, *, venv_pip: str,
                         run: Run = subprocess.run) -> list[str]:
    done: list[str] = []
    if changed_paths(repo, a, b, ["pyproject.toml", "uv.lock"], run=run):
        r = run([venv_pip, "install", "-e", ".[server]", "--quiet"],
                cwd=repo, capture_output=True, text=True)
        if r.returncode != 0:
            raise DeployError("update", f"pip install failed: {r.stderr.strip()}")
        done.append("pip")
    if changed_paths(repo, a, b, ["web/package-lock.json"], run=run):
        r = run(["npm", "ci"], cwd=os.path.join(repo, "web"), capture_output=True, text=True)
        if r.returncode != 0:
            raise DeployError("update", f"npm ci failed: {r.stderr.strip()}")
        done.append("npm")
    return done


def build_frontend(repo: str, run: Run = subprocess.run) -> None:
    r = run(["npm", "run", "build"], cwd=os.path.join(repo, "web"),
            capture_output=True, text=True)
    if r.returncode != 0:
        raise DeployError("update", f"frontend build failed: {r.stderr.strip()}")


def restart_units(*, daemon_changed: bool, daemon_enabled: bool,
                  run: Run = subprocess.run) -> list[str]:
    """Restart systemd units in order: daemon first (if changed+enabled), then edge.

    Order is load-bearing (browser reconnect-flush race, #190).
    Returns the ordered list of units restarted.
    """
    units: list[str] = []
    # DAEMON FIRST — order is load-bearing (browser reconnect-flush race, #190).
    if daemon_changed and daemon_enabled:
        run(["sudo", "systemctl", "restart", "ccswitch-daemon.service"],
            check=True, capture_output=True)
        units.append("ccswitch-daemon.service")
    run(["sudo", "systemctl", "restart", "ccswitch.service"], check=True, capture_output=True)
    units.append("ccswitch.service")
    return units


def _default_fetch(url: str) -> str:
    """Fetch URL body with a 3s timeout; return empty string on error."""
    import httpx
    try:
        return httpx.get(url, timeout=3.0).text
    except Exception:
        return ""


def wait_healthy(expected_sha: str, *, url: str, attempts: int = 15, sleep_s: float = 2.0,
                 fetch: Callable[[str], str] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> bool:
    """Poll url for health status; return True when body contains status:ok AND expected sha.

    Args:
        expected_sha: SHORT git sha to match in body.
        url: Health check endpoint URL.
        attempts: Number of poll attempts (default 15).
        sleep_s: Seconds between polls (default 2.0).
        fetch: Callable(url) -> str; defaults to httpx.get().text (empty string on error).
        sleep: Callable(seconds); defaults to time.sleep.

    Returns:
        True if both "status":"ok" and "sha":"<expected_sha>" are found in response body.
        False if attempts exhausted without both conditions met.
    """
    fetch = fetch or _default_fetch
    needle_status = '"status":"ok"'
    needle_sha = f'"sha":"{expected_sha}"'
    for _ in range(attempts):
        body = fetch(url)
        if needle_status in body and needle_sha in body:
            return True
        sleep(sleep_s)
    return False


def rollback_to(repo: str, sha: str, *, daemon_changed: bool, daemon_enabled: bool,
                run: Run = subprocess.run) -> None:
    """Reset to a known-good commit, rebuild frontend (best-effort), restart both units together.

    Args:
        repo: Path to repo root.
        sha: Git commit SHA to reset to.
        daemon_changed: Whether daemon code changed (controls daemon restart).
        daemon_enabled: Whether daemon systemd unit is enabled.
        run: Callable for subprocess execution (defaults to subprocess.run).

    Behavior:
        1. git reset --hard <sha> — first, reset to the good commit.
        2. Rebuild frontend best-effort — a rollback build failure only leaves web/dist stale;
           it must not raise (catches DeployError).
        3. Restart BOTH units together via restart_units() so they run the same (old) code —
           never split-version.
    """
    run(["git", "-C", repo, "reset", "--hard", sha], check=True, capture_output=True)
    # Rebuild best-effort — a rollback build failure only leaves web/dist stale; never raise.
    try:
        build_frontend(repo, run=run)
    except DeployError:
        pass
    # Restart BOTH together so they run the same (old) code — split-version is worse.
    restart_units(daemon_changed=daemon_changed, daemon_enabled=daemon_enabled, run=run)


def _default_gh_api_json(path: str, body: str) -> str:
    r = subprocess.run(["gh", "api", "-X", "POST", path, "--input", "-"],
                       input=body, check=True, capture_output=True, text=True)
    return r.stdout


def publish_deployment(sha: str, gh_api_json: Callable[[str, str], str] | None = None) -> bool:
    gh_api_json = gh_api_json or _default_gh_api_json
    try:
        dep_body = json.dumps({
            "ref": sha, "environment": "production", "production_environment": True,
            "auto_merge": False, "required_contexts": [],
        })
        dep = json.loads(gh_api_json("repos/{owner}/{repo}/deployments", dep_body))
        dep_id = dep.get("id")
        if not dep_id:
            return False
        gh_api_json(f"repos/{{owner}}/{{repo}}/deployments/{dep_id}/statuses",
                    json.dumps({"state": "success", "auto_inactive": True}))
        return True
    except Exception:
        return False
