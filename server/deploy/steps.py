from __future__ import annotations
import json
import os
import logging
import re
import subprocess
import time
from typing import Callable

from server.deploy.types import DeployError

log = logging.getLogger(__name__)

Run = Callable[..., subprocess.CompletedProcess]
_OK_CONCLUSIONS = {"success", "neutral", "skipped"}


def _default_gh_api(path: str) -> str:
    r = subprocess.run(["gh", "api", path], check=True, capture_output=True, text=True)
    return r.stdout


def _required_contexts(gh_api: Callable[[str], str], branch: str = "master") -> set[str]:
    """Required status-check contexts protecting ``branch`` (the deploy gate = merge gate).

    Empty set if branch protection is unreadable — the API needs admin scope, so a
    non-admin token degrades to "count every check" (the historical, fail-safe behavior:
    a non-required failure can only *block*, never spuriously pass). Deploy always targets
    origin/master (see ``git.py``), so this reads master's protection directly rather than
    resolving a default branch.

    Mirrors the required-contexts read in ``server.github.actions_api.repo_ci_meta`` (the
    CI/CD-doctor path); that one takes an explicit repo slug via a different gh seam, so
    deploy keeps this thin reader on its own injectable ``gh_api`` seam. Same endpoint,
    same meaning of "required".
    """
    try:
        data = json.loads(gh_api(
            f"repos/{{owner}}/{{repo}}/branches/{branch}/protection/required_status_checks"))
    except Exception:
        return set()
    return set(data.get("contexts") or [])


def ci_verdict(sha: str, gh_api: Callable[[str], str] | None = None) -> str:
    gh_api = gh_api or _default_gh_api
    try:
        body = json.loads(gh_api(f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs"))
    except Exception:
        return "unavailable"
    runs = body.get("check_runs") or []
    required = _required_contexts(gh_api)
    if required:
        # Only the checks that gate a MERGE gate a DEPLOY. Without this, a non-required or
        # flaky check (e.g. e2e) that never blocks a merge still wedges every self-deploy
        # (#620). A required context with no run yet is "pending", not "green".
        present = {r.get("name") for r in runs}
        if not required <= present:
            return "pending"
        runs = [r for r in runs if r.get("name") in required]
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


# Code the deploy controller itself executes. restart_units restarts the edge and
# the daemon, but the controller is the process RUNNING the deploy — it cannot
# restart itself inline without killing that deploy, so it never did. The result
# was a controller executing whatever code it started with: on 2026-08-21 #719's
# remote-daemon update landed on disk at 18:18 and the controller kept running its
# 16:56 code, so the next deploy left rocky on a stale commit — exactly the failure
# #719 exists to prevent.
_DEPLOY_CODE = re.compile(r"^server/deploy/")


def deploy_code_changed(changed_files: list[str] | None) -> bool:
    """True when this deploy modifies the controller's own code.

    Note the explicit empty/None handling: ``all([])`` is True, which is how
    classify_change silently reads an empty diff as edge-only. An empty diff
    changes nothing, so it cannot have changed deploy code.
    """
    if not changed_files:
        return False
    return any(_DEPLOY_CODE.match(f) for f in changed_files)


def schedule_controller_restart(*, run: Run = subprocess.run, delay_s: int = 10) -> bool:
    """Schedule a DEFERRED restart of ccswitch-deploy-controller.service.

    Deferred on purpose: the controller is the process performing the deploy, so
    an inline restart would kill the deploy that asked for it. systemd-run
    --on-active hands the restart to systemd and returns immediately, letting the
    current deploy finish and report before the process is replaced.

    Returns True when the restart was scheduled. Never raises: the deploy has
    already succeeded by this point and must not be failed by bookkeeping — but a
    False return means the controller is still running old code, so the caller
    surfaces it as a warning rather than assuming it worked.
    """
    try:
        run(["sudo", "systemd-run", f"--on-active={delay_s}s",
             "--unit=ccswitch-controller-refresh",
             "systemctl", "restart", "ccswitch-deploy-controller.service"],
            check=True, capture_output=True)
        return True
    except Exception:
        log.warning("deploy: could not schedule controller restart — it will keep "
                    "running the previous deploy code until restarted manually",
                    exc_info=True)
        return False


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


def _clear_incoming_untracked_collisions(repo: str, ref: str, run: Run = subprocess.run) -> list[str]:
    """Remove untracked working-tree files that collide with paths tracked in origin/<ref>.

    A file created out-of-band on the deploy host (untracked) but later committed upstream
    blocks `git merge --ff-only` ("untracked working tree files would be overwritten by
    merge"). Such a file is safe to drop: origin/<ref> carries the canonical tracked version
    that the merge is about to install. Only paths that exist in origin/<ref> are removed —
    unrelated untracked state (SQLite DB, .venv, web/dist) is left untouched.

    Returns the list of removed paths.
    """
    others = run(["git", "-C", repo, "ls-files", "--others", "--exclude-standard", "-z"],
                 capture_output=True, text=True)
    removed: list[str] = []
    for path in (p for p in others.stdout.split("\0") if p):
        # Only delete if the path is tracked in the incoming ref (i.e. will be replaced).
        present = run(["git", "-C", repo, "cat-file", "-e", f"origin/{ref}:{path}"],
                      capture_output=True, text=True)
        if present.returncode == 0:
            run(["git", "-C", repo, "clean", "-f", "-q", "--", path],
                check=True, capture_output=True)
            removed.append(path)
    return removed


def fast_forward(repo: str, ref: str = "master", run: Run = subprocess.run) -> None:
    """Fast-forward the local branch to origin/<ref>.

    Deliberately fetches and then fast-forwards to the *tracking ref* (origin/<ref>)
    rather than using `git pull` (which merges the shared FETCH_HEAD). The deploy
    controller and the 30s DetectLoop both fetch this same worktree's FETCH_HEAD;
    two concurrent `git fetch origin <ref>` duplicate the for-merge line, so a
    `git pull --ff-only` intermittently dies with "Cannot fast-forward to multiple
    branches". origin/<ref> is a single unambiguous ref, immune to that race.

    If an untracked file collides with a path newly tracked in origin/<ref>, the
    ff-merge aborts ("untracked working tree files would be overwritten by merge");
    that file is removed and the merge retried once (#320: a health-check script was
    placed out-of-band before its commit landed, wedging every deploy permanently).

    Raises DeployError("update", ...) if the fetch or the fast-forward fails.
    """
    f = run(["git", "-C", repo, "fetch", "--quiet", "origin", ref],
            capture_output=True, text=True)
    if f.returncode != 0:
        raise DeployError("update", f"fetch failed: {f.stderr.strip()}")
    m = run(["git", "-C", repo, "merge", "--ff-only", f"origin/{ref}"],
            capture_output=True, text=True)
    if m.returncode != 0 and "untracked working tree files would be overwritten" in m.stderr:
        if _clear_incoming_untracked_collisions(repo, ref, run):
            m = run(["git", "-C", repo, "merge", "--ff-only", f"origin/{ref}"],
                    capture_output=True, text=True)
    if m.returncode != 0:
        raise DeployError("update", f"fast-forward failed: {m.stderr.strip()}")


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


# ssh treats a leading '-' in a positional argument as an OPTION, not a hostname (there is
# no "--" end-of-options convention that helps: ssh has no way to say "the next arg is
# definitely the host"). An unvalidated CCSWITCH_DAEMON_SSH_HOST could therefore smuggle
# arbitrary ssh flags — e.g. "-oProxyCommand=...|sh" — into a sudo-privileged deploy command.
# Require the optional user and the host to each START with an alphanumeric so a leading
# '-' (or any other flag-introducing character) is structurally impossible.
_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(@[A-Za-z0-9][A-Za-z0-9_.-]*)?$")
# The remote checkout path also lands in an argv position on the far side of ssh.
# Absolute, and no shell metacharacters, separators or leading '-' (option smuggling).
_SSH_REPO_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
# A git rev passed to `git merge --ff-only`. Hex only, never a ref expression.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def restart_units(*, daemon_changed: bool, daemon_enabled: bool,
                  target_sha: str | None = None,
                  run: Run = subprocess.run) -> list[str]:
    """Restart systemd units in order: daemon first (if changed), then edge.

    Order is load-bearing (browser reconnect-flush race, #190).

    The daemon may live on a remote host (see rocky session-host cutover): when
    ``CCSWITCH_DAEMON_SSH_HOST`` is set, the daemon restart is issued over SSH instead of
    locally, and the local ``daemon_enabled`` guard is bypassed for it — dobby's local unit
    is deliberately *disabled* once the daemon has moved to rocky, so gating on it would
    make every deploy silently restart nothing and leave rocky running stale code (the
    Postgres cutover incident this change exists to prevent). ``daemon_changed`` still gates
    the remote restart: it reflects whether daemon-relevant files changed in this deploy's
    diff, so skipping an unneeded restart avoids unnecessary daemon churn.

    A remote daemon has its OWN checkout, so this also UPDATES that checkout before
    restarting it. Restarting alone just relaunches the same stale commit: on 2026-08-21,
    deploying #715 gave dobby's edge the ``cloned_repos`` RPC caller while rocky's daemon
    stayed on f310aa6 without the handler, and every repo listing returned
    ``409 {"detail": "cloned_repos"}``. The remote is fast-forwarded (never reset) to
    *target_sha*, so an operator's local commits abort the deploy instead of being lost.

    Requires ``CCSWITCH_DAEMON_SSH_REPO`` (the checkout path on the remote host) whenever a
    remote daemon actually needs restarting. Missing or unsafe values raise before any
    command runs — a deploy that cannot update the remote must fail, not silently restart it.

    Returns the ordered list of units restarted.
    """
    units: list[str] = []
    daemon_host = os.environ.get("CCSWITCH_DAEMON_SSH_HOST")
    if daemon_host and not _SSH_HOST_RE.match(daemon_host):
        # Fail loud, before issuing ANY command (including the edge restart below) — a
        # deploy that cannot safely restart the remote daemon must stop, not silently fall
        # back to a local restart. Falling back would resurrect the exact silent-no-op
        # hazard this feature exists to prevent, and would mask a misconfiguration or attack.
        raise DeployError(
            "restart",
            f"CCSWITCH_DAEMON_SSH_HOST={daemon_host!r} is not a valid [user@]host value; "
            "refusing to build an ssh command from it",
        )
    # Validate everything the remote path needs BEFORE running anything, so a
    # misconfiguration cannot leave the edge restarted against a stale daemon.
    daemon_repo = os.environ.get("CCSWITCH_DAEMON_SSH_REPO")
    if daemon_host and daemon_changed:
        if not daemon_repo:
            raise DeployError(
                "restart",
                "CCSWITCH_DAEMON_SSH_HOST is set but CCSWITCH_DAEMON_SSH_REPO is not; "
                "refusing to restart a remote daemon whose code this deploy cannot update "
                "(a restarted-but-stale daemon serves the previous commit's RPC surface)",
            )
        if not _SSH_REPO_RE.match(daemon_repo):
            raise DeployError(
                "restart",
                f"CCSWITCH_DAEMON_SSH_REPO={daemon_repo!r} is not a safe absolute path; "
                "refusing to build an ssh command from it",
            )
        if target_sha is not None and not _SHA_RE.match(target_sha):
            raise DeployError(
                "restart",
                f"target_sha={target_sha!r} is not a hex git sha; "
                "refusing to build an ssh command from it",
            )

    # DAEMON FIRST — order is load-bearing (browser reconnect-flush race, #190).
    if daemon_host:
        if daemon_changed:
            assert daemon_repo is not None  # guaranteed by the validation above
            # Update the remote checkout, then restart. Fast-forward only: a diverged
            # remote aborts the deploy rather than losing whatever is there.
            run(["ssh", daemon_host, "git", "-C", daemon_repo, "fetch", "--quiet", "origin"],
                check=True, capture_output=True)
            run(["ssh", daemon_host, "git", "-C", daemon_repo, "merge", "--ff-only",
                 target_sha or "origin/master"],
                check=True, capture_output=True)
            run(["ssh", daemon_host, "sudo", "systemctl", "restart", "ccswitch-daemon.service"],
                check=True, capture_output=True)
            units.append("ccswitch-daemon.service")
    elif daemon_changed and daemon_enabled:
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


def _gh_err_reason(e: Exception) -> str:
    """Concise one-line failure reason from a gh/subprocess error (prefers stderr tail).

    subprocess.CalledProcessError carries the useful diagnostic on .stderr (e.g. the
    ``gh: HTTP 307`` a POST hits when the repo was renamed and the git remote is stale);
    str(e) alone is just "returned non-zero exit status 1". Fall back to the exception
    text, then the class name, so the surfaced warning is never empty.
    """
    stderr = getattr(e, "stderr", None)
    if isinstance(stderr, str) and stderr.strip():
        return stderr.strip().splitlines()[-1]
    return str(e).strip() or e.__class__.__name__


def publish_deployment(sha: str, gh_api_json: Callable[[str, str], str] | None = None) -> str | None:
    """Record a GitHub Deployment for ``sha``. Returns None on success, else a short reason.

    Best-effort: the caller surfaces the reason as a deploy warning but never fails the
    deploy. The reason is captured (not swallowed) so a recurring failure — e.g. a stale
    git remote after a repo rename making the POST 307 — is diagnosable from the deploy UI.
    """
    gh_api_json = gh_api_json or _default_gh_api_json
    try:
        dep_body = json.dumps({
            "ref": sha, "environment": "production", "production_environment": True,
            "auto_merge": False, "required_contexts": [],
        })
        dep = json.loads(gh_api_json("repos/{owner}/{repo}/deployments", dep_body))
        dep_id = dep.get("id")
        if not dep_id:
            return "no deployment id in response"
        gh_api_json(f"repos/{{owner}}/{{repo}}/deployments/{dep_id}/statuses",
                    json.dumps({"state": "success", "auto_inactive": True}))
        return None
    except Exception as e:
        return _gh_err_reason(e)
