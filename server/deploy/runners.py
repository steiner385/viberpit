"""Per-runner deploy lifecycle (spec §2): snapshot → update checkout → (drain, restart)
→ verify → restore, for every host in the fleet registry, local or ssh-reachable.

Transport rules (constitution IV — everything in ssh argv is validated first):
  * remote checkouts move by ``merge --ff-only <sha>`` only; ``reset --hard`` exists solely
    for the rollback rewind;
  * pip is ``<repo>/.venv/bin/python -m pip`` (never ``bin/pip``, #860) and only when the
    lockfile changed;
  * ``sync`` after every checkout write, before the restart (2026-08-29 zero-byte module).

A runner is restarted on a daemon-changing deploy, or when its OWN running process lags
the target in daemon code (#3290): ``steps._EDGE_ONLY`` guarantees an edge-only diff
touched no daemon-imported Python, but the diff is the EDGE's, so a runner that failed an
earlier deploy before its restart is still on older daemon code the edge's diff no longer
shows. The checkout is updated on EVERY deploy so ``/info.checkoutSha`` matches the
deployed commit and the skew gate (spec §4) stays green.

Before a remote checkout moves, ``_preflight_checkout`` refuses one that is off
``RUNNER_BRANCH`` or carries local commits, naming the cause instead of surfacing git's
"Not possible to fast-forward" advice (#3289). It diagnoses only; it never repairs.

``deploy_runners`` runs at most ``concurrency`` runners at once (decision 2a: 2) and
isolates failures — one runner's failure never stops another (decision 1a).
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from server.deploy import drain as _drain
from server.host_units import resolve_unit
from server.deploy.steps import (
    LOCAL_DAEMON_UNIT_FILE, REMOTE_DAEMON_UNIT_FILE, SSH_HOST_RE, SSH_REL_PATH_RE, SSH_REPO_RE,
    _SHA_RE, check_daemon_unit_drift, classify_change, deps_changed as _deps_changed,
    short_sha_match,
)

log = logging.getLogger("server.deploy.runners")

Run = Callable[..., subprocess.CompletedProcess]
Fetch = Callable[[str], Awaitable[dict]]
Progress = Callable[[str, "HostProgress"], None]
# Changed files between a sha a runner's PROCESS reports and the deploy target, computed
# in the edge's repo (None = uncomputable). The controller supplies it; see _lag_plan.
LagDiff = Callable[[str], "list[str] | None"]

# The branch a runner checkout must be on: the one `merge --ff-only <target>` advances.
RUNNER_BRANCH = "master"
# How many local commits a checkout-preflight refusal names before summarising.
_PREFLIGHT_COMMITS_SHOWN = 5

SSH_OPTS = ("-oBatchMode=yes", "-oConnectTimeout=10")
VERIFY_TIMEOUT_S = 180.0
VERIFY_INTERVAL_S = 3.0
# restore_deploy re-launches every snapshotted session serially in the fresh daemon, one
# full resume each. Rocky took 132s for four on 2026-10-14, against the client's 30s
# default, and the deploy reported "restore call failed" for a restore that succeeded
# (#3082). The read timeout has to cover a busy host, not the median.
RESTORE_TIMEOUT_S = 600.0
# Pauses between restore attempts. Safe to retry: the daemon's restore_deploy is
# single-flight, so a retry joins the run in flight or reads its result.
RESTORE_RETRY_DELAYS_S: tuple[float, ...] = (5.0, 20.0)
DEFAULT_CONCURRENCY = 2

SKIPPED_STATES = frozenset({"offline", "disabled"})
FAILED_STATES = frozenset({"failed", "unconfigured"})


@dataclass(frozen=True)
class RunnerTarget:
    name: str
    endpoint: str
    enabled: bool
    healthy: bool | None
    ssh_host: str | None
    repo_path: str | None
    # The probe's last_error, verbatim. Load-bearing: `healthy is False` alone cannot tell
    # "unreachable" from "reached fine, but FENCED by the skew/protocol gate" — see
    # is_deployable.
    last_error: str | None = None


@dataclass
class HostProgress:
    state: str = "pending"        # pending | running | done | failed | offline | disabled | unconfigured
    step: str | None = None
    error: str | None = None
    sha: str | None = None
    moved: bool = False           # checkout advanced — a rollback must rewind it
    # This deploy set out to restart the daemon (set before the drain), whether because
    # the deploy changed daemon code or because this runner's process lagged (#3290). A
    # rollback restarts exactly these runners.
    restarted: bool = False
    restore: dict | None = None
    # Non-fatal findings the controller lifts into the deploy's warnings (unit drift).
    warnings: list[str] = field(default_factory=list)

    def to_wire(self) -> dict:
        return {"state": self.state, "step": self.step, "error": self.error, "sha": self.sha,
                "restore": self.restore}


def _venv_python(repo_path: str) -> str:
    return f"{repo_path}/.venv/bin/python"


def _validate_repo(repo_path: str) -> None:
    if not SSH_REPO_RE.match(repo_path):
        raise ValueError(f"repo_path {repo_path!r} is not a safe absolute path")


def _validate(repo_path: str, sha: str) -> None:
    _validate_repo(repo_path)
    if not _SHA_RE.match(sha):
        raise ValueError(f"sha {sha!r} is not a hex git sha")


def remote_update_commands(repo_path: str, target_sha: str, *,
                           deps_changed: bool) -> list[tuple[str, list[str]]]:
    """Ordered (step, remote argv) to bring a runner's checkout to *target_sha*."""
    _validate(repo_path, target_sha)
    cmds: list[tuple[str, list[str]]] = [
        ("fetch", ["git", "-C", repo_path, "fetch", "--quiet", "origin"]),
        ("merge", ["git", "-C", repo_path, "merge", "--ff-only", target_sha]),
    ]
    if deps_changed:
        # Single-quoted: ssh joins argv into one remote shell line and `[server]` is a
        # glob there. The path is SSH_REPO_RE-validated, so the quotes are inert.
        cmds.append(("pip", [_venv_python(repo_path), "-m", "pip", "install", "-e",
                             f"'{repo_path}[server]'", "--quiet"]))
    cmds.append(("sync", ["sync"]))
    return cmds


_DIRTY_MERGE_HEADER = "local changes to the following files would be overwritten"
_DIRTY_MERGE_FOOTER = "Please commit your changes"


def local_change_collisions(stderr: str) -> list[str]:
    """The TRACKED paths a `git merge --ff-only` named as locally modified, or [].

    A runner checkout is deploy-managed state, not a workspace, but things still write to
    it: one `npm install` anywhere in the tree rewrites `web/package-lock.json`, and from
    then on every upgrade of that host aborts with "Your local changes to the following
    files would be overwritten by merge" (rocky, 2026-09-20 → 09-24). The edge repo never
    hits this because the controller calls ``steps.guard_working_tree`` first; runners had
    no equivalent.

    Only this one git message is recognised. The untracked-file variant reads almost the
    same but its remedy is DELETING files, not restoring them — conflating them here would
    let a failed merge quietly delete a runner's untracked state. That variant is handled
    separately by ``RunnerDeploy._clear_untracked_collisions`` (#2323), which sweeps and
    deletes remotely instead of parsing paths out of this stderr.

    Returns [] — recover nothing — if ANY named path fails ``SSH_REL_PATH_RE``. These
    paths go back out in ssh argv, which ssh joins into a remote shell line; a path git
    could not have produced means this stderr is not the message we take it for, and
    picking out the "safe half" would be reasoning about a string we no longer trust.
    """
    if _DIRTY_MERGE_HEADER not in stderr:
        return []
    paths: list[str] = []
    for line in stderr.splitlines():
        if _DIRTY_MERGE_HEADER in line:
            paths = []          # a second header: keep only the last block
            continue
        if _DIRTY_MERGE_FOOTER in line or line.startswith("Aborting"):
            break
        if line.startswith(("\t", "    ")):
            paths.append(line.strip())
    if not paths or not all(SSH_REL_PATH_RE.match(p) for p in paths):
        return []
    return paths


_UNTRACKED_MERGE_MARKER = "untracked working tree files would be overwritten"

# Remote-side mirror of steps._clear_incoming_untracked_collisions, run as ONE ssh call
# rather than one `cat-file -e` round trip per untracked path (the edge's version pays
# that cost as a local subprocess; over ssh it would be one network round trip each).
# {repo} and {rev} are embedded directly — both are validated (SSH_REPO_RE / _SHA_RE)
# by every caller before this template is formatted, so neither can carry shell
# metacharacters. $p is never interpolated into the script text: it flows through the
# shell's own `read`, so a path containing quotes or a colon cannot escape the "$p"
# argument it is read into.
_UNTRACKED_CLEAN_SCRIPT = (
    'git -C {repo} ls-files --others --exclude-standard -z | '
    'while IFS= read -r -d "" p; do '
    'if git -C {repo} cat-file -e {rev}:"$p" 2>/dev/null; then '
    'git -C {repo} clean -f -q -- "$p"; printf "%s\\0" "$p"; '
    'fi; done'
)

# Checkout preflight (#3289): are the files the checkout's local commits touch identical
# to {rev}? Exit 0 = yes (the commits were squash-merged, so the checkout is safe to move
# by hand); xargs exits 123 when the diff found a difference. {repo} and {rev} are
# validated by every caller, exactly as for _UNTRACKED_CLEAN_SCRIPT; paths flow through
# xargs -0, never through the script text.
_LOCAL_COMMITS_MERGED_SCRIPT = (
    'git -C {repo} diff --name-only -z {rev}...HEAD | '
    'xargs -0 -r git -C {repo} diff --quiet HEAD {rev} --'
)


def remote_rewind_commands(repo_path: str, sha: str) -> list[tuple[str, list[str]]]:
    """Rollback only: move a runner BACK to *sha* (ff-only cannot go backwards).

    A rollback sha lands in ssh argv exactly like a forward one, so it gets the same
    repo-path + hex-sha validation before any command runs.
    """
    _validate(repo_path, sha)
    return [
        ("fetch", ["git", "-C", repo_path, "fetch", "--quiet", "origin"]),
        ("reset", ["git", "-C", repo_path, "reset", "--hard", sha]),
        ("sync", ["sync"]),
    ]




def is_deployable(t: RunnerTarget) -> bool:
    """Is this host something a deploy should try to update?

    NOT the same question as "is it dispatchable". The probe sets healthy=False for two
    quite different situations:

    * the host could not be REACHED (``ConnectError: …``) — nothing to deploy to; and
    * the host answered perfectly well but is FENCED by the version-skew or protocol gate.

    The fenced case is precisely the host a deploy must fix. Treating `healthy is not True`
    as "offline" meant a runner that failed one deploy was silently skipped by every later
    one — and the deploy still reported green, so the host stayed fenced forever.
    """
    # Imported lazily: host_probe pulls in the ORM/session layer, which this module (and
    # its tests) otherwise never need.
    from server.sessions.host_probe import FENCED_REACHABLE_PREFIXES
    if not t.enabled:
        return False
    if t.healthy is True:
        return True
    return (t.healthy is False and t.last_error is not None
            and t.last_error.startswith(FENCED_REACHABLE_PREFIXES))


def is_local(target: RunnerTarget, local_name: str) -> bool:
    """The daemon on THIS host: its checkout is the controller's own repo (already updated
    by the `update` phase) and its unit restarts without ssh."""
    return target.ssh_host is None and target.name == local_name


def _default_client_factory(endpoint: str) -> Any:
    from server.ipc.client import DaemonClient
    from server.ipc.paths import parse_endpoint
    return DaemonClient(parse_endpoint(endpoint))


def _default_fetch_info() -> Fetch:
    from server.sessions.host_probe import default_fetch
    return default_fetch


class RunnerDeploy:
    def __init__(self, target: RunnerTarget, *, local_name: str, run: Run = subprocess.run,
                 client_factory: Callable[[str], Any] | None = None,
                 fetch_info: Fetch | None = None, drain_mod=_drain,
                 verify_timeout_s: float = VERIFY_TIMEOUT_S,
                 verify_interval_s: float = VERIFY_INTERVAL_S,
                 restore_retry_delays_s: tuple[float, ...] = RESTORE_RETRY_DELAYS_S,
                 on_progress: Progress | None = None):
        self.target = target
        self.local_name = local_name
        self._run = run
        self._client_factory = client_factory or _default_client_factory
        self._fetch_info: Fetch = fetch_info or _default_fetch_info()
        self._drain = drain_mod
        self._verify_timeout_s = verify_timeout_s
        self._verify_interval_s = verify_interval_s
        self._restore_retry_delays_s = restore_retry_delays_s
        self._on_progress = on_progress
        self.progress = HostProgress()

    # ---- bookkeeping -------------------------------------------------------------
    def _mark(self, state: str | None = None, step: str | None = None, **kw: Any) -> None:
        if state is not None:
            self.progress.state = state
        self.progress.step = step
        for k, v in kw.items():
            setattr(self.progress, k, v)
        if self._on_progress is not None:
            try:
                self._on_progress(self.target.name, self.progress)
            except Exception:
                log.debug("runner %s: on_progress callback failed", self.target.name, exc_info=True)

    # ---- transport ---------------------------------------------------------------
    async def _ssh(self, step: str, remote: list[str]) -> None:
        ssh_host = self.target.ssh_host
        if not ssh_host or not SSH_HOST_RE.match(ssh_host):
            raise RuntimeError(f"ssh_host {ssh_host!r} is not a valid [user@]host")
        r = await self._ssh_raw(remote)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip()[:500]
            raise RuntimeError(f"{step} failed (rc={r.returncode}): {detail}")

    async def _ssh_raw(self, remote: list[str]) -> subprocess.CompletedProcess:
        """Run *remote* over ssh and hand back the result — the caller decides if it failed."""
        ssh_host = self.target.ssh_host
        if not ssh_host or not SSH_HOST_RE.match(ssh_host):
            raise RuntimeError(f"ssh_host {ssh_host!r} is not a valid [user@]host")
        argv = ["ssh", *SSH_OPTS, ssh_host, *remote]
        return await asyncio.to_thread(self._run, argv, capture_output=True, text=True)

    async def _clear_untracked_collisions(self, repo_path: str, rev: str) -> list[str]:
        """Remote mirror of ``steps._clear_incoming_untracked_collisions``: sweep the
        checkout's untracked files, delete only those tracked at *rev*, and hand back
        what was removed. Runs as a single ssh call — see ``_UNTRACKED_CLEAN_SCRIPT``.
        """
        script = _UNTRACKED_CLEAN_SCRIPT.format(repo=repo_path, rev=rev)
        r = await self._ssh_raw(["bash", "-c", shlex.quote(script)])
        if r.returncode != 0:
            return []
        return [p for p in (r.stdout or "").split("\0") if p]

    async def _preflight_checkout(self, repo_path: str, target_sha: str) -> None:
        """Refuse a checkout ``merge --ff-only`` cannot honestly move, saying why (#3289).

        A runner checkout is deploy-managed, but a session can still do its PR work in it
        rather than a worktree. On 2026-11-04 rocky sat on a feature branch holding the
        UNSQUASHED commit of the very PR being deployed: same content, diverged history,
        and the merge failed with git's advice to `merge --no-ff` or `rebase` — both wrong
        for a runner. Two checks, before the merge and after the fetch (the target's
        objects must be local for the second):

        * HEAD must be ``RUNNER_BRANCH``. Off it, even a SUCCESSFUL ff-only merge would
          advance the wrong branch.
        * ``<target>..HEAD`` must be empty. Local commits are listed, with whether their
          content is already in the target (the squash-merged, safe-to-move case).

        Diagnostics only (constitution IV: no ``reset --hard`` outside rollback). Whether
        the operator's work is safe to discard is not ours to decide.
        """
        problems: list[str] = []
        r = await self._ssh_raw(["git", "-C", repo_path, "symbolic-ref", "--short", "-q", "HEAD"])
        branch = (r.stdout or "").strip()
        if r.returncode == 1:
            problems.append(f"HEAD is detached, not on {RUNNER_BRANCH}")
        elif r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip()[:300]
            raise RuntimeError(f"checkout preflight failed (rc={r.returncode}): {detail}")
        elif branch != RUNNER_BRANCH:
            problems.append(f"it is on branch {branch!r}, not {RUNNER_BRANCH}")

        log_r = await self._ssh_raw(["git", "-C", repo_path, "log", "--format=%h%x09%s",
                                     f"{target_sha}..HEAD"])
        if log_r.returncode != 0:
            detail = (log_r.stderr or log_r.stdout or "").strip()[:300]
            raise RuntimeError(f"checkout preflight failed (rc={log_r.returncode}): {detail}")
        commits = [c.replace("\t", " ", 1) for c in (log_r.stdout or "").splitlines() if c]
        if commits:
            shown = "; ".join(commits[:_PREFLIGHT_COMMITS_SHOWN])
            more = len(commits) - _PREFLIGHT_COMMITS_SHOWN
            listing = shown + (f"; +{more} more" if more > 0 else "")
            problems.append(f"it has {len(commits)} local commit(s) not in "
                            f"{target_sha[:12]} ({listing})")
            script = _LOCAL_COMMITS_MERGED_SCRIPT.format(repo=repo_path, rev=target_sha)
            same = await self._ssh_raw(["bash", "-c", shlex.quote(script)])
            if same.returncode == 0:
                problems.append(f"their changes are already in {target_sha[:12]} "
                                "(squash-merged?), so the checkout is safe to move by hand")
            else:
                problems.append(f"their changes differ from {target_sha[:12]}: make sure "
                                "they are pushed before moving the checkout")
        if problems:
            raise RuntimeError(
                f"runner checkout {repo_path} cannot be fast-forwarded: "
                + "; ".join(problems)
                + f". A deploy only fast-forwards a clean {RUNNER_BRANCH}: put the checkout on "
                f"{self.target.name} back on {RUNNER_BRANCH} by hand once its work is safe "
                f"(e.g. `git -C {repo_path} checkout {RUNNER_BRANCH}`), then re-run the deploy")

    async def _merge(self, repo_path: str, remote: list[str], target_sha: str) -> None:
        """The `merge` step, with one recovery for a dirty checkout.

        Exactly one retry, and only after clearing exactly what git named as the
        collision:

        * tracked paths locally modified — restored with `git checkout -- <paths>`, since
          the merge about to run installs the canonical version of every one of them;
        * untracked paths that collide with a path newly tracked at *target_sha* — deleted
          via ``_clear_untracked_collisions`` (#2323), since the merge installs the
          tracked version in their place.

        Anything else dirty in the tree is left alone and still fails the runner loudly —
        matching ``runner_sync``'s stance that an operator's work in a checkout is not
        ours to discard.
        """
        r = await self._ssh_raw(remote)
        if r.returncode == 0:
            return
        detail = (r.stderr or r.stdout or "").strip()
        paths = local_change_collisions(detail)
        if paths:
            log.warning("runner %s: checkout dirty, restoring %s and retrying the merge once",
                        self.target.name, ", ".join(paths))
            await self._ssh("restore", ["git", "-C", repo_path, "checkout", "--", *paths])
        elif _UNTRACKED_MERGE_MARKER in detail:
            removed = await self._clear_untracked_collisions(repo_path, target_sha)
            if not removed:
                raise RuntimeError(f"merge failed (rc={r.returncode}): {detail[:500]}")
            log.warning("runner %s: untracked-file collision, removed %s and retrying the "
                        "merge once", self.target.name, ", ".join(removed))
        else:
            raise RuntimeError(f"merge failed (rc={r.returncode}): {detail[:500]}")
        r2 = await self._ssh_raw(remote)
        if r2.returncode != 0:
            detail2 = (r2.stderr or r2.stdout or "").strip()[:500]
            raise RuntimeError(f"merge failed (rc={r2.returncode}): {detail2}")

    async def _local(self, step: str, argv: list[str]) -> None:
        r = await asyncio.to_thread(self._run, argv, capture_output=True, text=True)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip()[:500]
            raise RuntimeError(f"{step} failed (rc={r.returncode}): {detail}")

    def _prefix(self, *, local: bool) -> list[str]:
        """The argv that runs a command on this runner's host: none locally, ssh remotely."""
        if local:
            return []
        host = self.target.ssh_host
        if not host or not SSH_HOST_RE.match(host):
            raise RuntimeError(f"ssh_host {host!r} is not a valid [user@]host")
        return ["ssh", *SSH_OPTS, host]

    async def _restart(self, *, local: bool) -> None:
        prefix = self._prefix(local=local)
        unit = await asyncio.to_thread(resolve_unit, "daemon", run=self._run, prefix=prefix)
        if local:
            await self._local("restart", ["sudo", "systemctl", "restart", unit])
        else:
            await self._ssh("restart", ["sudo", "-n", "systemctl", "restart", unit])

    async def _close_quietly(self, client: Any) -> None:
        """Release a per-call daemon client — one helper, every call site.

        Each client owns two ``httpx.AsyncClient``s, their transports and (on the TLS
        branch) certifi-loaded SSL contexts; left unclosed they hold their sockets until
        GC, the leak ``DaemonClient.aclose`` exists to prevent.

        Tolerant by design, and load-bearingly so: this runs inside ``deploy``'s try, so a
        raising ``aclose`` would mark the runner FAILED — and since the fan-out gates the
        deploy, fail the whole deploy over teardown. The factory is also an injection
        point whose test doubles are plain objects with only ``call``, so a missing
        ``aclose`` must be a no-op. Best-effort, logged at debug.
        """
        closer = getattr(client, "aclose", None)
        if closer is None:
            return
        try:
            await closer()
        except Exception:  # noqa: BLE001 — teardown must never fail a deploy
            log.debug("runner %s: client teardown failed", self.target.name, exc_info=True)

    async def _rpc(self, method: str, **kw: Any) -> dict:
        client = self._client_factory(self.target.endpoint)
        try:
            return await client.call(method, **kw)
        finally:
            await self._close_quietly(client)

    async def _drain_then(self) -> None:
        """Drain in-flight turns (bounded by the deploy caps) and drop the edge's /events
        socket so the old daemon can exit on SIGTERM promptly (#740). Best-effort."""
        self._mark(step="drain")
        client = self._client_factory(self.target.endpoint)
        try:
            result = await self._drain.drain_and_wait(client)
        finally:
            await self._close_quietly(client)
        if result != "ready":
            log.warning("runner %s: drain %s — restarting anyway", self.target.name, result)
        try:
            await self._rpc("close_events_connections")
        except Exception:
            log.debug("runner %s: close_events_connections failed (best-effort)",
                      self.target.name, exc_info=True)

    async def _restore(self, snapshot_ids: list[str]) -> dict:
        """Call restore_deploy, retrying with backoff. Never raises.

        On final failure the result carries ``error`` and ``unconfirmed`` (the
        snapshotted ids, whose restore outcome is unknown) so the controller can
        raise an alert that names them. The daemon keeps retrying any session it
        could not resume on its own (``_resume_recovery``); this only covers the call.
        """
        attempts = len(self._restore_retry_delays_s) + 1
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return await self._rpc("restore_deploy", timeout=RESTORE_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 — retried, then reported, never raised
                last = exc
                log.warning("runner %s: restore_deploy attempt %d/%d failed: %s",
                            self.target.name, attempt + 1, attempts, exc,
                            exc_info=attempt + 1 == attempts)
                if attempt + 1 < attempts:
                    await asyncio.sleep(self._restore_retry_delays_s[attempt])
        return {"restored": [], "failed": [], "unconfirmed": list(snapshot_ids),
                "error": f"restore call failed after {attempts} attempts: "
                         f"{type(last).__name__}: {last}"[:300]}

    async def _verify(self, target_sha: str, *, require_process: bool) -> str:
        """Poll /info until the checkout (and, after a restart, the process) is on target."""
        self._mark(step="verify")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._verify_timeout_s
        while True:
            try:
                info = await self._fetch_info(self.target.endpoint)
                checkout = info.get("checkoutSha") or info.get("sha")
                process = info.get("sha")
                ok = bool(checkout) and short_sha_match(str(checkout), target_sha)
                if ok and require_process:
                    ok = bool(process) and short_sha_match(str(process), target_sha)
                if ok:
                    return str(process) if require_process else str(checkout)
            except Exception:
                pass  # daemon still restarting — keep polling until the deadline
            if loop.time() >= deadline:
                raise RuntimeError(
                    f"verify failed: daemon did not report {target_sha} within "
                    f"{int(self._verify_timeout_s)}s")
            await asyncio.sleep(self._verify_interval_s)

    async def _lag_plan(self, target_sha: str, lag_diff: LagDiff) -> tuple[bool, bool]:
        """(restart, reinstall deps) for THIS runner on a deploy whose own diff is edge-only.

        The deploy's classification is the edge's ``local..target`` diff. A runner that
        failed an earlier deploy before its restart is still running older daemon code that
        diff no longer covers, so it was never restarted however often the deploy re-ran
        (#3290: rocky after 729b166). Classify this runner's own lag instead: the diff from
        the sha its process reports on /info to the target.

        An unreadable /info, or a process sha that is not a commit ("unknown" without a
        .git), leaves the runner alone with a warning: a restart could not be verified
        against such a sha, so it would only interrupt sessions to fail the verify.
        """
        try:
            info = await self._fetch_info(self.target.endpoint)
        except Exception as exc:  # noqa: BLE001 — unknown lag is reported, not fatal
            self.progress.warnings.append(f"could not read /info to check daemon lag: {exc}"[:300])
            return False, False
        process = str(info.get("sha") or "")
        if process and short_sha_match(process, target_sha):
            return False, False
        if not _SHA_RE.match(process):
            self.progress.warnings.append(
                f"daemon reports sha {process!r}, so whether it lags {target_sha[:12]} is unknown")
            return False, False
        changed = await asyncio.to_thread(lag_diff, process)
        restart = classify_change(changed) == "daemon_changed"
        if restart:
            log.info("runner %s: process on %s lags %s in daemon code — restarting it",
                     self.target.name, process, target_sha)
        return restart, _deps_changed(changed)

    # ---- the lifecycle -----------------------------------------------------------
    async def deploy(self, *, target_sha: str, daemon_changed: bool, deps_changed: bool,
                     local_daemon_enabled: bool,
                     lag_diff: LagDiff | None = None) -> HostProgress:
        """One runner's lifecycle. *daemon_changed*/*deps_changed* are the deploy's own
        (edge-diff) verdicts; with *lag_diff* a runner whose process lags the target in
        daemon code is restarted even when they are False (see ``_lag_plan``)."""
        t = self.target
        if not t.enabled:
            self._mark("disabled")
            return self.progress
        if not is_deployable(t):
            self._mark("offline")
            return self.progress
        local = is_local(t, self.local_name)
        if local and not local_daemon_enabled:
            self._mark("disabled")
            return self.progress
        repo_path: str | None = None
        if not local:
            if not t.ssh_host or not t.repo_path:
                self._mark("unconfigured",
                           error=f"host {t.name!r} has no ssh_host/repo_path — cannot update it")
                return self.progress
            if not SSH_HOST_RE.match(t.ssh_host):
                self._mark("failed", error=f"ssh_host {t.ssh_host!r} is not a valid [user@]host")
                return self.progress
            repo_path = t.repo_path
        try:
            self._mark("running", step="snapshot")
            # The ids the restore must answer for: if every restore attempt fails, these
            # are the sessions whose fate is unknown, and the alert has to name them.
            snapshot_ids: list[str] = []
            try:
                snap = await self._rpc("snapshot_deploy")
                snapshot_ids = [str(e["id"]) for e in (snap or {}).get("live", [])
                                if isinstance(e, dict) and e.get("id")]
            except Exception:
                log.warning("runner %s: session snapshot failed (continuing)", t.name, exc_info=True)

            restart, reinstall = daemon_changed, deps_changed
            if not daemon_changed and lag_diff is not None:
                self._mark(step="lag-check")
                lag_restart, lag_deps = await self._lag_plan(target_sha, lag_diff)
                restart, reinstall = lag_restart, deps_changed or lag_deps

            if not local:
                assert repo_path is not None
                for step, remote in remote_update_commands(repo_path, target_sha,
                                                           deps_changed=reinstall):
                    if step == "merge":
                        self._mark(step="checkout")
                        await self._preflight_checkout(repo_path, target_sha)
                        self._mark(step=step)
                        await self._merge(repo_path, remote, target_sha)
                        self.progress.moved = True
                    else:
                        self._mark(step=step)
                        await self._ssh(step, remote)

            if restart:
                self.progress.restarted = True
                await self._drain_then()
                self._mark(step="restart")
                await self._restart(local=local)

            sha = await self._verify(target_sha, require_process=restart)
            self.progress.sha = sha

            if restart:
                self._mark(step="restore")
                self.progress.restore = await self._restore(snapshot_ids)
            self._mark(step="unit-drift")
            self.progress.warnings += await self._unit_drift(local=local)
            self._mark("done")
            log.info("deploy.runner.done host=%s sha=%s restarted=%s", t.name, sha, restart)
        except Exception as exc:  # noqa: BLE001 — a runner failure is a REPORTED state
            self._mark("failed", step=self.progress.step, error=str(exc)[:500])
            log.warning("deploy.runner.failed host=%s step=%s: %s", t.name, self.progress.step, exc)
        return self.progress

    async def _unit_drift(self, *, local: bool) -> list[str]:
        """Safety directives the repo declares but this host's installed unit lacks (#2702).

        Checked on every deploy, restart or not: the drift is a standing condition, and the
        deploy is the one moment that already knows the committed unit changed. Never
        fails the runner — the remedy is sudo on the host, not a retry.
        """
        unit_file = LOCAL_DAEMON_UNIT_FILE if local else REMOTE_DAEMON_UNIT_FILE
        try:
            return await asyncio.to_thread(check_daemon_unit_drift, unit_file,
                                           prefix=self._prefix(local=local), run=self._run)
        except Exception:
            log.debug("runner %s: unit drift check failed", self.target.name, exc_info=True)
            return []

    async def rewind(self, sha: str, *, daemon_changed: bool) -> None:
        """Rollback: put a moved remote runner back on *sha*. Never raises.

        The CHECKOUT always moves back — the deploy advanced it on every runner, whatever
        the diff touched. The DAEMON is only drained and restarted when this deploy
        restarted it: an edge-only deploy left the process on its prior commit (which is
        *sha*), so restarting here would kill live turns to reach a state already true.
        The drain is why the restart is conditional and not merely wasteful — a bare
        restart takes in-flight turns down with it.
        """
        t = self.target
        if is_local(t, self.local_name) or not t.ssh_host or not t.repo_path:
            return
        try:
            for step, remote in remote_rewind_commands(t.repo_path, sha):
                self._mark("running", step=f"rewind:{step}")
                await self._ssh(step, remote)
            if daemon_changed:
                await self._drain_then()
                self._mark(step="rewind:restart")
                await self._restart(local=False)
            await self._verify(sha, require_process=daemon_changed)
            self._mark("done", sha=sha)
        except Exception as exc:  # noqa: BLE001
            self._mark("failed", error=f"rewind failed: {exc}"[:500])
            log.error("deploy.runner.rewind_failed host=%s: %s", t.name, exc)


async def deploy_runners(targets: list[RunnerTarget], *, target_sha: str, daemon_changed: bool,
                         deps_changed: bool, local_name: str, local_daemon_enabled: bool,
                         concurrency: int = DEFAULT_CONCURRENCY,
                         on_progress: Progress | None = None,
                         lag_diff: LagDiff | None = None,
                         **runner_kwargs: Any) -> dict[str, HostProgress]:
    """Deploy every target, at most *concurrency* at a time. Returns name → progress."""
    sem = asyncio.Semaphore(max(1, concurrency))
    deploys = {t.name: RunnerDeploy(t, local_name=local_name, on_progress=on_progress,
                                    **runner_kwargs) for t in targets}

    async def one(rd: RunnerDeploy) -> None:
        async with sem:
            await rd.deploy(target_sha=target_sha, daemon_changed=daemon_changed,
                            deps_changed=deps_changed, local_daemon_enabled=local_daemon_enabled,
                            lag_diff=lag_diff)

    await asyncio.gather(*(one(rd) for rd in deploys.values()), return_exceptions=True)
    return {name: rd.progress for name, rd in deploys.items()}


async def find_lagging_runners(targets: list[RunnerTarget], *, target_sha: str,
                               local_name: str, local_daemon_enabled: bool,
                               fetch_info: Fetch | None = None) -> list[RunnerTarget]:
    """The deployable runners whose process or checkout is NOT on *target_sha* (#3290).

    What a deploy with nothing new for the edge still has to do: a runner that failed the
    last deploy kept its old code while the edge moved on. A host whose /info cannot be
    read, or reports no sha, is left out — there is no evidence it lags.
    """
    fetch = fetch_info or _default_fetch_info()
    candidates = [t for t in targets if is_deployable(t)
                  and (local_daemon_enabled or not is_local(t, local_name))]

    async def lags(t: RunnerTarget) -> bool:
        try:
            info = await fetch(t.endpoint)
        except Exception:  # noqa: BLE001 — unreachable is not evidence of lag
            log.debug("runner %s: /info unreadable during lag check", t.name, exc_info=True)
            return False
        shas = [str(s) for s in (info.get("sha"), info.get("checkoutSha")) if s]
        return any(not short_sha_match(s, target_sha) for s in shas)

    verdicts = await asyncio.gather(*(lags(t) for t in candidates))
    return [t for t, lag in zip(candidates, verdicts) if lag]


async def rewind_runners(targets: list[RunnerTarget], results: dict[str, HostProgress], *,
                         sha: str, local_name: str, daemon_changed: bool,
                         **runner_kwargs: Any) -> None:
    """Rollback helper: rewind every runner whose checkout this deploy moved.

    A runner is restarted on the way back exactly when the deploy restarted it: the
    deploy's own classification (*daemon_changed*) or that runner's own lag restart
    (``HostProgress.restarted``) — no more.
    """
    moved = [t for t in targets if results.get(t.name) is not None and results[t.name].moved]
    await asyncio.gather(
        *(RunnerDeploy(t, local_name=local_name, **runner_kwargs).rewind(
            sha, daemon_changed=daemon_changed or results[t.name].restarted) for t in moved),
        return_exceptions=True)


async def load_runner_targets() -> list[RunnerTarget]:
    """Every fleet host row (enabled or not — the lifecycle reports `disabled` itself)."""
    from sqlalchemy import select as sa_select
    from server.db import db_session
    from server.db.models import Host
    async with db_session() as db:
        rows = (await db.execute(sa_select(Host).order_by(Host.name))).scalars().all()
    return [RunnerTarget(name=r.name, endpoint=r.endpoint, enabled=bool(r.enabled),
                         healthy=r.healthy, ssh_host=r.ssh_host, repo_path=r.repo_path,
                         last_error=r.last_error)
            for r in rows]
