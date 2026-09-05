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

# The deploy controller only ever targets its own repo, whose owner gh resolves from
# the git remote when it expands the ``{owner}/{repo}`` path template. Naming it here
# lets the gate mint an App installation token for that owner (issue #912/#913) instead
# of using whatever ambient credential the environment happens to hold.
_DEPLOY_OWNER = "steiner385"


def _gh_api_via_app(args: list[str], *, input: str | None = None) -> str:
    """Run ``gh <args>`` as the deploy repo's App installation, returning stdout.

    Issue #913: the previous ``subprocess.run(["gh", ...])`` used the raw process
    environment, so a stale ambient ``GITHUB_TOKEN`` was honoured and the gate broke
    the moment it expired. ``gitrepos._run_gh`` strips ambient tokens, pins the gh
    config dir, and sets the App token when one is available (ambient/pinned fallback
    otherwise). Non-zero still raises ``CalledProcessError`` — the contract
    ``_gh_err_reason`` and the ``ci_verdict``/``publish_deployment`` callers rely on
    (they read ``.stderr`` and treat any raise as unavailable)."""
    from ccswitch import gitrepos

    r = gitrepos._run_gh(args, token=gitrepos.app_token_for_owner(_DEPLOY_OWNER), input=input)
    if r.returncode != 0:
        raise subprocess.CalledProcessError(
            r.returncode, r.args, output=r.stdout, stderr=r.stderr)
    return r.stdout


def _default_gh_api(path: str) -> str:
    return _gh_api_via_app(["api", path])


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


def _never_ran(sha: str, gh_api: Callable[[str], str]) -> bool:
    """True when no further check-run can ever arrive for ``sha``.

    GitHub allows at most ONE *pending* run per concurrency group and cancels the
    previously pending one; ``cancel-in-progress: false`` only protects a run that has
    already started. A run cancelled while still pending starts zero jobs, so it
    contributes no check-run to the commit — ever. On the check-runs endpoint alone that
    is indistinguishable from "the run has not started yet", which is why a missing
    required context used to read as a "pending" that could never resolve.

    The workflow-runs endpoint separates them: every run for the sha finished AND at
    least one was cancelled means the absent context is not coming. Best-effort — any
    unreadable or ambiguous answer returns False, leaving the caller on the historical
    "pending". Both verdicts block a deploy; only the wording an operator sees differs.
    """
    try:
        body = json.loads(gh_api(f"repos/{{owner}}/{{repo}}/actions/runs?head_sha={sha}"))
    except Exception:
        return False
    runs = body.get("workflow_runs") or []
    if not runs or any(r.get("status") != "completed" for r in runs):
        return False
    return any((r.get("conclusion") or "") == "cancelled" for r in runs)


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
            # ...unless it will never report at all, which is "unrun" — a distinct,
            # terminal verdict. Only this branch pays for the extra round-trip.
            return "unrun" if _never_ran(sha, gh_api) else "pending"
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


# Code the deploy controller itself executes. restart_edge (and, for runners,
# server.deploy.runners) restart the edge and the daemon, but the controller is
# the process RUNNING the deploy — it cannot
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


# Units a deploy restarts on THIS host: the edge, the local runner daemon, and the
# controller itself (deferred, when deploy code changed). Remote runners are checked by
# their own host's controller, not from here.
RESTARTABLE_UNITS: tuple[str, ...] = (
    "ccswitch.service",
    "ccswitch-daemon.service",
    "ccswitch-deploy-controller.service",
)

# `systemctl show -p ExecStart <unit>` renders as:
#   ExecStart={ path=/x/.venv/bin/python ; argv[]=/x/.venv/bin/python -m server.ipc ; ... }
_EXEC_PATH_RE = re.compile(r"path=([^\s;]+)")


def _checkout_root(path: str) -> str | None:
    """The checkout directory ``path`` nominally belongs to, or None.

    The convention is ``<checkout>/.venv/bin/python``. A path not shaped like that (e.g.
    a future service-owned venv artifact) has no notion of "which checkout owns it", so
    the isolation check has no positive proof to act on and must not fire.
    """
    marker = "/.venv/"
    idx = path.find(marker)
    return path[:idx] if idx != -1 else None


def check_unit_interpreters(units: tuple[str, ...] | None = None,
                            *, run: Run = subprocess.run) -> list[str]:
    """Return one problem string per unit whose ExecStart binary cannot be executed.

    A unit can keep RUNNING long after its ExecStart binary has vanished — a live process
    holds open inodes, not paths — so this breakage is invisible until something tries to
    exec it again. A deploy restarts these units, which makes the deploy itself the thing
    that discovers the problem, at the worst possible moment: 2026-09-04, the daemon's
    `.venv` was a symlink into a checkout that got renamed, and the next deploy would have
    stopped every live session with nothing able to start again.

    Unresolvable units are SKIPPED, never reported. If systemctl fails, the unit is not
    installed, or the property does not parse, we have no *evidence* of breakage — and a
    preflight that refuses on absent evidence is precisely the failure mode that wedged
    the deploy gate behind a required check which could never report. Fail only on
    positive proof: a path we resolved that is missing or not executable.

    Also reports a unit whose interpreter is *alive* but has escaped its own checkout:
    ExecStart named ``<checkout>/.venv/bin/python``, yet ``.venv`` resolves (by symlink)
    into some other checkout entirely (issue #1171 — the coupling #1129 above only
    detects once the *other* checkout has already broken). That still runs today, but
    renaming or rebuilding the other checkout takes this unit down with no warning.
    """
    problems: list[str] = []
    for unit in (units if units is not None else RESTARTABLE_UNITS):
        try:
            proc = run(["systemctl", "show", "-p", "ExecStart", unit],
                       check=False, capture_output=True, text=True)
        except Exception:
            continue                      # no systemd / not permitted — unverifiable
        raw = (getattr(proc, "stdout", "") or "")
        match = _EXEC_PATH_RE.search(raw)
        if not match:
            continue                      # absent unit or unparseable — unverifiable
        path = match.group(1)
        # os.path.exists FOLLOWS symlinks, which is the point: the real failure was a
        # symlink whose target moved. lexists would call that healthy.
        if not os.path.exists(path):
            problems.append(
                f"{unit}: ExecStart binary {path} does not exist "
                "(the unit is running only because a live process holds its open files; "
                "it cannot restart)")
        elif not os.access(path, os.X_OK):
            problems.append(f"{unit}: ExecStart binary {path} is not executable")
        else:
            root = _checkout_root(path)
            resolved = os.path.realpath(path)
            if root is not None and resolved != path and not resolved.startswith(root + os.sep):
                problems.append(
                    f"{unit}: ExecStart binary {path} (checkout {root}) resolves to "
                    f"{resolved}, a different checkout entirely — this unit's interpreter "
                    "is owned by another developer checkout and breaks if that checkout "
                    "is renamed, rebuilt, or removed")
    return problems


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


def _clear_incoming_untracked_collisions(repo: str, rev: str, run: Run = subprocess.run) -> list[str]:
    """Remove untracked working-tree files that collide with paths tracked in *rev*
    (the fast-forward's merge target: ``origin/<ref>`` or an explicit sha).

    A file created out-of-band on the deploy host (untracked) but later committed upstream
    blocks `git merge --ff-only` ("untracked working tree files would be overwritten by
    merge"). Such a file is safe to drop: *rev* carries the canonical tracked version
    that the merge is about to install. Only paths that exist in *rev* are removed —
    unrelated untracked state (SQLite DB, .venv, web/dist) is left untouched.

    Returns the list of removed paths.
    """
    others = run(["git", "-C", repo, "ls-files", "--others", "--exclude-standard", "-z"],
                 capture_output=True, text=True)
    removed: list[str] = []
    for path in (p for p in others.stdout.split("\0") if p):
        # Only delete if the path is tracked in the incoming rev (i.e. will be replaced).
        present = run(["git", "-C", repo, "cat-file", "-e", f"{rev}:{path}"],
                      capture_output=True, text=True)
        if present.returncode == 0:
            run(["git", "-C", repo, "clean", "-f", "-q", "--", path],
                check=True, capture_output=True)
            removed.append(path)
    return removed


def fast_forward(repo: str, ref: str = "master", run: Run = subprocess.run) -> None:
    """Fast-forward the local branch to origin/<ref>, or to an explicit commit sha.

    *ref* may be a branch name (the default deploy: fast-forward to ``origin/<ref>``)
    or a hex commit sha (green-frontier deploy: fast-forward to exactly that commit,
    stopping short of a red ``origin/master`` tip — ``git merge --ff-only <sha>`` works
    the same). A sha cannot be fetched by name, so the fetch still targets ``master``.

    Deliberately fetches and then fast-forwards to the *tracking ref* (origin/<ref>)
    rather than using `git pull` (which merges the shared FETCH_HEAD). The deploy
    controller and the 30s DetectLoop both fetch this same worktree's FETCH_HEAD;
    two concurrent `git fetch origin <ref>` duplicate the for-merge line, so a
    `git pull --ff-only` intermittently dies with "Cannot fast-forward to multiple
    branches". origin/<ref> is a single unambiguous ref, immune to that race — and an
    explicit sha is immune by construction.

    If an untracked file collides with a path newly tracked in the merge target, the
    ff-merge aborts ("untracked working tree files would be overwritten by merge");
    that file is removed and the merge retried once (#320: a health-check script was
    placed out-of-band before its commit landed, wedging every deploy permanently).

    Raises DeployError("update", ...) if the fetch or the fast-forward fails.
    """
    is_sha = bool(_SHA_RE.match(ref))
    fetch_ref = "master" if is_sha else ref
    target_rev = ref if is_sha else f"origin/{ref}"
    f = run(["git", "-C", repo, "fetch", "--quiet", "origin", fetch_ref],
            capture_output=True, text=True)
    if f.returncode != 0:
        raise DeployError("update", f"fetch failed: {f.stderr.strip()}")
    m = run(["git", "-C", repo, "merge", "--ff-only", target_rev],
            capture_output=True, text=True)
    if m.returncode != 0 and "untracked working tree files would be overwritten" in m.stderr:
        if _clear_incoming_untracked_collisions(repo, target_rev, run):
            m = run(["git", "-C", repo, "merge", "--ff-only", target_rev],
                    capture_output=True, text=True)
    if m.returncode != 0:
        raise DeployError("update", f"fast-forward failed: {m.stderr.strip()}")


def changed_paths(repo: str, a: str, b: str, paths: list[str], run: Run = subprocess.run) -> list[str]:
    r = run(["git", "-C", repo, "diff", "--name-only", a, b, "--", *paths],
            capture_output=True, text=True)
    return [p for p in r.stdout.splitlines() if p.strip()]


def sync_deps_if_changed(repo: str, a: str, b: str, *, venv_python: str,
                         run: Run = subprocess.run) -> list[str]:
    done: list[str] = []
    if changed_paths(repo, a, b, ["pyproject.toml", "uv.lock"], run=run):
        # Invoke pip as a module of the venv's interpreter, never via the `bin/pip`
        # launcher script: that launcher bakes the venv's ABSOLUTE path into its shebang
        # at creation time, so a venv that was later moved (or is reached through a
        # symlink) has a launcher pointing at an interpreter that no longer exists —
        # exec fails with ENOENT *for the launcher itself*, which looks like "pip is
        # missing" while `bin/pip` sits right there (2026-08-30 upgrade failure).
        # `bin/python` is a symlink and survives a move; `-m pip` needs nothing else.
        r = run([venv_python, "-m", "pip", "install", "-e", ".[server,stt]", "--quiet"],
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
# Public: the fleet host-upgrade path (server/sessions/host_upgrade.py and the
# /api/fleet PATCH validation) builds ssh commands from operator-supplied values and
# MUST validate with these same patterns — one source of truth for "safe ssh argv".
SSH_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(@[A-Za-z0-9][A-Za-z0-9_.-]*)?$")
# The remote checkout path also lands in an argv position on the far side of ssh.
# Absolute, and no shell metacharacters, separators or leading '-' (option smuggling).
SSH_REPO_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
# A git rev passed to `git merge --ff-only`. Hex only, never a ref expression.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

# One source of truth for the local daemon-unit restart argv: server.deploy.runners
# imports this as its own LOCAL_RESTART rather than re-literalling the same list.
LOCAL_DAEMON_RESTART: list[str] = ["sudo", "systemctl", "restart", "ccswitch-daemon.service"]


def restart_edge(*, run: Run = subprocess.run) -> list[str]:
    """Restart the edge unit. Runner (daemon) restarts — local AND remote — are owned by
    ``server.deploy.runners`` and run BEFORE this (daemon-before-edge order is load-bearing:
    the browser reconnect-flush race, #190). Returns the units restarted."""
    run(["sudo", "systemctl", "restart", "ccswitch.service"], check=True, capture_output=True)
    return ["ccswitch.service"]


def restart_local_daemon(*, run: Run = subprocess.run) -> None:
    run(LOCAL_DAEMON_RESTART, check=True, capture_output=True)


def _default_fetch(url: str) -> str:
    """Fetch URL body with an 8s timeout; return empty string on error.

    The timeout must outlast a *healthy* /healthz, not just a fast one. In remote mode
    /healthz makes daemon RPCs (active_session_count, x11_metrics) that answer quickly once
    the daemon is reachable but can lag for a few seconds while the daemon warms up and the
    edge re-attaches over a settling tailnet right after a restart. A 3s timeout gave up
    before such a slow-but-healthy response arrived, so the gate saw only empty bodies and
    rolled back a healthy deploy (#780). 8s clears that warmup latency while still capping how
    long a genuinely-wedged /healthz (~10s of connect timeouts, daemon:"unavailable") drags
    each poll — so a dead daemon still rolls back in bounded time.
    """
    import httpx
    try:
        return httpx.get(url, timeout=8.0).text
    except Exception:
        return ""


def remote_runners_configured(hosts=None) -> bool:
    """True when the fleet has an enabled REMOTE runner (a host with ``ssh_host``).

    Sizes the health window: after its own restart the edge must re-attach to remote
    daemons over a settling tailnet even on an edge-only deploy (#780). *hosts* is the
    ``load_runner_targets()`` list; None → False (single-host install).
    """
    return any(h.enabled and h.ssh_host for h in (hosts or []))


# Health-gate poll windows. wait_healthy returns on the FIRST healthy poll, so a
# longer ceiling costs nothing when the deploy is fine — it is only spent when
# something is actually wrong.
HEALTH_POLL_INTERVAL_S = 2.0
# Single-host edge-only deploy (NO remote daemon): nothing slow to wait for, so fail fast.
# This short window applies only when ``remote_runners_configured()`` is False; with a remote
# daemon the edge must re-attach to it after restart, so the controller uses the longer
# window below even for an edge-only deploy (see controller.deploy_once).
HEALTH_ATTEMPTS_EDGE_ONLY = 15
# A remote daemon is in play. Either it was restarted (`systemctl restart` over ssh returns
# when the process STARTS, not when uvicorn is SERVING, and rocky needs ~10-30s more to bind
# its port), OR — even on an edge-only deploy — the restarted edge must re-attach to it over a
# settling tailnet (#780). The first real deploy after #726 failed at the 30s ceiling and
# rolled back with nothing actually wrong; #780 rolled back an edge-only deploy for the same
# reason. Both need this longer window.
HEALTH_ATTEMPTS_WITH_DAEMON = 75


def wait_healthy(expected_sha: str, *, url: str, attempts: int = 15, sleep_s: float = 2.0,
                 require_daemon_sha: bool = True,
                 fetch: Callable[[str], str] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> bool:
    """Poll url for health status; return True when body contains status:ok AND expected sha.

    Args:
        expected_sha: SHORT git sha to match in body.
        url: Health check endpoint URL.
        attempts: Number of poll attempts (default 15).
        sleep_s: Seconds between polls (default 2.0).
        require_daemon_sha: assert the daemon is at *expected_sha* too. Pass
            ``daemon_changed`` here: only a deploy that actually advanced the remote
            daemon may require it to be on the target commit (see below).
        fetch: Callable(url) -> str; defaults to httpx.get().text (empty string on error).
        sleep: Callable(seconds); defaults to time.sleep.

    Also gates on the DAEMON, not just the edge. In remote mode /healthz degrades
    gracefully when the daemon is unreachable — active=0, daemon="unavailable" —
    and still returns status ok with the right sha, so an edge-only gate passes a
    deploy whose daemon is dead. Every production failure on 2026-08-21/22 passed
    this gate: a stale rocky daemon 409ing every repo listing, a daemon hung in
    startup that never bound its port, and a corrupt ledger.

    Two extra conditions, both read from the SAME response (no new round-trip):
      * daemon "unavailable" is unhealthy — the flag was already there, ignored.
      * daemonSha, when reported AND *require_daemon_sha*, must match expected_sha.
        That is the #719 skew class: an edge deployed with a new RPC caller while
        the daemon still lacks the handler. But an EDGE-ONLY deploy
        (daemon_changed=False) never touches the daemon — server.deploy.runners skips it —
        so it stays on its previous (older) commit by design. Requiring it to be on
        the target then rolls back every edge-only deploy once the daemon has
        drifted behind the edge (the 2026-08-26 #776 rollback). So the sha
        assertion is conditional on the daemon having actually been advanced;
        availability is always required.

    Both degrade open when absent: a single-host deploy reports no daemon key, and
    a daemon too old to report its sha must not brick deploys.

    Returns:
        True if the edge is ok at expected_sha AND the daemon is not unavailable
        AND (when require_daemon_sha) any reported daemonSha matches.
        False if attempts exhausted without all conditions met.
    """
    fetch = fetch or _default_fetch
    needle_status = '"status":"ok"'
    needle_sha = f'"sha":"{expected_sha}"'
    for _ in range(attempts):
        body = fetch(url)
        if (needle_status in body and needle_sha in body
                and _daemon_ok(body, expected_sha, require_sha=require_daemon_sha)):
            return True
        sleep(sleep_s)
    return False


def short_sha_match(a: str, b: str) -> bool:
    """True if two git short-SHAs denote the same commit despite differing length.

    ``git rev-parse --short`` emits the shortest prefix that is UNIQUE within a
    single repository's object database, so the SAME commit abbreviates to
    different lengths on different hosts: on 2026-08-25 dobby rendered f26c56ee475…
    as ``f26c56ee`` (8 chars — a colliding object forced the extra nibble) while
    rocky rendered it ``f26c56e`` (7). The edge's expected sha is abbreviated on the
    edge host and the daemon's sha on the daemon host, so exact ``==`` wrongly
    reported skew and rolled back a deploy where both hosts were on the right
    commit. Either abbreviation of one commit is a prefix of the other, so match on
    prefix rather than equality — a genuinely different commit shares no such prefix
    and is still rejected (the #719 skew class).
    """
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def _daemon_ok(body: str, expected_sha: str, *, require_sha: bool = True) -> bool:
    """True unless the body positively reports an unhealthy or skewed daemon.

    Parsed, not substring-matched: `"daemonSha":"<sha>"` contains `"sha":"<sha>"`
    as a substring, so naive matching would let the edge's own sha check pass off
    the daemon's value (or vice versa). Unparseable bodies fall through to the
    caller's other needles rather than failing here.

    *require_sha* gates only the daemonSha comparison. An edge-only deploy leaves
    the daemon on its prior commit deliberately, so it must still be reachable but
    is not expected to be on the target sha — see ``wait_healthy``.
    """
    try:
        doc = json.loads(body)
    except Exception:
        return True
    if not isinstance(doc, dict):
        return True
    if doc.get("daemon") == "unavailable":
        return False
    if require_sha:
        daemon_sha = doc.get("daemonSha")
        if daemon_sha and not short_sha_match(str(daemon_sha), expected_sha):
            return False
    return True


# Functional endpoints the deploy probes after the health gate. /healthz proves the
# process is up and the DB schema matches; it never reads the ledger or the session
# store, so a corrupt ledger left /api/accounts 500ing while /healthz stayed green
# (2026-08-22). These are the cheapest endpoints that exercise those paths.
# /api/github/repos exercises the gh listing + the daemon cloned_repos RPC — the
# path whose failure blanked the New Session repo dropdown (2026-08-29); note the
# probe is unauthenticated, so its 401 only proves the route answers (4xx passes).
DEFAULT_PROBE_PATHS: tuple[str, ...] = ("/api/sessions", "/api/accounts", "/api/github/repos")


def _default_status_fetch(url: str) -> int:
    """GET *url* and return its status code. Raises on transport failure."""
    import httpx
    return httpx.get(url, timeout=10.0).status_code


def probe_endpoints(base_url: str, paths, *, attempts: int = 3, sleep_s: float = 2.0,
                    fetch_status=None, sleep=time.sleep) -> list[str]:
    """Probe *paths* under *base_url*; return the ones still failing.

    A failure is 5xx or an unreachable endpoint. 4xx is NOT a failure: where the
    probe is unauthenticated, 401/403 means the app was reached and answered,
    which is exactly what this checks. Failing on 4xx would turn the safety check
    into an outage on any authenticating deployment.

    Retries within *attempts* so an app still warming up does not fail a deploy on
    its first response.
    """
    fetch_status = fetch_status or _default_status_fetch
    failed: list[str] = []
    for path in paths:
        url = base_url.rstrip("/") + path
        ok = False
        for attempt in range(attempts):
            try:
                code = fetch_status(url)
                if code < 500:
                    ok = True
                    break
                log.warning("deploy probe %s -> %s (attempt %d/%d)",
                            path, code, attempt + 1, attempts)
            except Exception as exc:  # noqa: BLE001 — unreachable counts as failing
                log.warning("deploy probe %s unreachable: %s (attempt %d/%d)",
                            path, exc, attempt + 1, attempts)
            if attempt < attempts - 1:
                sleep(sleep_s)
        if not ok:
            failed.append(path)
    return failed


def require_endpoints_healthy(base_url: str, paths=None, **kw) -> None:
    """probe_endpoints, raising DeployError when anything failed.

    Named `require_` rather than `assert_`: unittest.mock reserves the `assert_`
    prefix and raises AttributeError for any unrecognised `assert_*` attribute (so
    typos like `assert_called_once` fail loudly). A Steps method named that way
    breaks every Mock-based fake in the suite.

    Raises rather than warning: a deploy whose functional endpoints are 500ing has
    not succeeded, and a warning in a status blob is exactly how the corrupt-ledger
    outage stayed invisible.
    """
    bad = probe_endpoints(base_url, paths or DEFAULT_PROBE_PATHS, **kw)
    if bad:
        raise DeployError(
            "restart",
            f"post-deploy probe failed for {', '.join(bad)} — the app is running "
            f"but these endpoints are erroring, so the deploy is not healthy",
        )


def rollback_to(repo: str, sha: str, *, daemon_changed: bool, daemon_enabled: bool,
                run: Run = subprocess.run) -> None:
    """Reset the EDGE checkout to a known-good commit, rebuild the frontend (best-effort)
    and restart the local units. Remote runners this deploy moved are rewound by the
    controller via ``runners.rewind_runners`` — the two together restore one version
    fleet-wide (2026-08-25: rollback left rocky on the rejected commit)."""
    run(["git", "-C", repo, "reset", "--hard", sha], check=True, capture_output=True)
    try:
        build_frontend(repo, run=run)
    except DeployError:
        pass
    if daemon_changed and daemon_enabled:
        restart_local_daemon(run=run)
    restart_edge(run=run)


def _default_gh_api_json(path: str, body: str) -> str:
    return _gh_api_via_app(["api", "-X", "POST", path, "--input", "-"], input=body)


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
