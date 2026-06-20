"""SessionOrchestrator — ties SessionManager, UsagePoller, and rotation together.

The API and WS layers call this.  One instance lives on app.state.

Usage:
    orch = SessionOrchestrator(claude_bin=[sys.executable, "fake.py"])
    await orch.start()
    record = await orch.create_session("My session")
    ...
    await orch.stop()
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Callable

from ccswitch import configdir, worktrees
from ccswitch.configdir import compose
from server.db import db_session
from server.db.models import Session as DbSession
from server.sessions.manager import SessionManager
from server.sessions.rotation import RotationDeps, rotate
from server.sessions.selection import AllAccountsBusy, select_account
from server.sessions.types import SessionRecord
from server.usage.poller import UsagePoller

log = logging.getLogger(__name__)


class BranchSwitchError(Exception):
    """Raised when a git branch checkout fails."""


# Default: use load_config().claude_bin, overridden in tests
_DEFAULT_CLAUDE_BIN: str = "claude"

# Session-window utilisation ceiling that triggers rotation (100 = hard ceiling per spec)
_ROTATION_CEILING: float = 100.0


class SessionOrchestrator:
    """Orchestrates session lifecycle: creation, rotation, termination.

    Args:
        claude_bin: argv prefix for spawning the claude process.  Defaults to
            ``["claude"]`` (resolved via PATH).  Override in tests:
            ``[sys.executable, "tests/fakes/fake_claude_pty.py"]``.
        work_dir: Default cwd for new sessions.  Defaults to the process cwd.
        poll_interval: Seconds between usage-poller sweeps (default 60).
        usage_fn: Override for the usage-fetch function (testing).
    """

    def __init__(
        self,
        claude_bin: list[str] | None = None,
        work_dir: str | None = None,
        poll_interval: float = 60.0,
        usage_fn: Callable[[str], object] | None = None,
        repo_root: str | None = None,
    ) -> None:
        if claude_bin is None:
            # Resolve from env (matches ccswitch.accounts.add_account logic)
            bin_str = os.environ.get("CCSWITCH_CLAUDE_BIN", _DEFAULT_CLAUDE_BIN)
            import shlex
            claude_bin = shlex.split(bin_str)
        self._claude_bin: list[str] = claude_bin
        self._work_dir: str = work_dir or os.getcwd()
        # Root under which per-session worktrees are created (<root>/.ccswitch/...).
        # Defaults to the configured repo_root so worktrees sit next to the repos.
        if repo_root is None:
            try:
                from server.config import get_settings
                repo_root = get_settings().repo_root
            except Exception:
                repo_root = os.path.expanduser("~/GitHub")
        self._repo_root: str = repo_root
        # session_id → WorktreeInfo for sessions launched in an isolated worktree
        self._worktrees: dict[str, worktrees.WorktreeInfo] = {}
        self._manager: SessionManager = SessionManager()
        self._poller: UsagePoller = UsagePoller(
            poll_interval=poll_interval,
            usage_fn=usage_fn,
        )
        # In-memory map: session_id → SessionRecord
        self._records: dict[str, SessionRecord] = {}
        # Background tasks
        self._rotation_task: asyncio.Task | None = None
        # Event bus — WS bridge subscribes here for lifecycle events
        self._event_subscribers: list[asyncio.Queue] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the usage poller and the rotation monitor."""
        await self._poller.start()
        self._rotation_task = asyncio.create_task(
            self._rotation_monitor(), name="rotation-monitor"
        )

    async def stop(self) -> None:
        """Stop the rotation monitor and usage poller."""
        if self._rotation_task is not None:
            self._rotation_task.cancel()
            try:
                await self._rotation_task
            except asyncio.CancelledError:
                pass
            self._rotation_task = None
        await self._poller.stop()

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    async def create_session(
        self,
        name: str,
        account: str | None = None,
        initial_prompt: str | None = None,
        cols: int = 80,
        rows: int = 24,
        cwd: str | None = None,
        branch: str | None = None,
    ) -> SessionRecord:
        """Pick an account, spawn a claude PTY, persist a DB row, return a SessionRecord.

        Raises:
            AllAccountsBusy: when no account has headroom.
            ValueError: when cwd is given but does not exist.
            BranchSwitchError: when branch checkout fails.
        """
        # Resolve cwd: if given, it MUST be an existing directory
        if cwd is not None:
            if not os.path.isdir(cwd):
                raise ValueError(f"cwd does not exist: {cwd}")
        else:
            cwd = self._work_dir

        session_id = str(uuid.uuid4())

        # Per-session isolation: when a branch is selected on a git repo, run the
        # session in its OWN worktree checked out to that branch instead of doing
        # a `git checkout` in the user's real tree. Two sessions on one repo never
        # collide, and the real checkout is left untouched. (Falls back to a child
        # branch if the selected branch is already checked out elsewhere.)
        worktree_info: worktrees.WorktreeInfo | None = None
        if branch is not None and worktrees.is_git_repo(cwd):
            try:
                worktree_info = await asyncio.to_thread(
                    worktrees.create_worktree, cwd, branch, session_id, self._repo_root
                )
            except worktrees.WorktreeError as exc:
                raise BranchSwitchError(str(exc)) from exc
            cwd = worktree_info.path
            self._worktrees[session_id] = worktree_info

        if account is None:
            account = select_account()
        else:
            # Verify the given account exists (will raise if cred file missing — acceptable)
            pass

        cfg_dir: Path = compose(account)

        # Pre-trust the project directory (now the worktree, if any) so claude
        # won't prompt for trust on startup.
        configdir.trust_project(account, cwd)

        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg_dir)}
        argv = list(self._claude_bin)

        await self._manager.spawn(
            session_id=session_id,
            argv=argv,
            env=env,
            cwd=cwd,
            cols=cols,
            rows=rows,
        )

        # Best-effort: grab the pid from the internal session
        pid: int | None = None
        _s = self._manager._sessions.get(session_id)  # type: ignore[attr-defined]
        if _s is not None:
            pid = _s.pid

        # Capture claude_session_id (newest *.jsonl under cfg_dir/projects/**)
        claude_session_id: str | None = _find_claude_session_id(cfg_dir, cwd)

        # Write initial_prompt to PTY
        if initial_prompt:
            await self._manager.write_input(session_id, initial_prompt.encode())
            if not initial_prompt.endswith("\n"):
                await self._manager.write_input(session_id, b"\n")

        # Persist DB row
        async with db_session() as db:
            row = DbSession(
                id=session_id,
                name=name,
                account_name=account,
                claude_session_id=claude_session_id,
                status="working",
                pid=pid,
                cwd=cwd,
            )
            db.add(row)

        record = SessionRecord(
            id=session_id,
            name=name,
            account=account,
            claude_session_id=claude_session_id or "",
            cols=cols,
            rows=rows,
            cwd=cwd,
        )
        self._records[session_id] = record
        log.info("session.create id=%s account=%s pid=%s", session_id, account, pid)
        return record

    async def close_session(self, session_id: str) -> None:
        """Terminate the PTY, clean up the session worktree, mark the row ended."""
        await self._manager.terminate(session_id)
        record = self._records.pop(session_id, None)

        # Remove the per-session worktree if we created one (or if the session's
        # cwd is one of our managed worktree paths — recognised even after a
        # server restart). Dirty worktrees are kept; remove_worktree never
        # destroys uncommitted work.
        wt = self._worktrees.pop(session_id, None)
        wt_path = wt.path if wt else (
            record.cwd if record and worktrees.is_managed_path(record.cwd, self._repo_root)
            else None
        )
        if wt_path:
            try:
                removed = await asyncio.to_thread(worktrees.remove_worktree, wt_path)
                log.info("session.worktree id=%s removed=%s path=%s",
                         session_id, removed, wt_path)
            except Exception:
                log.exception("session.worktree_cleanup_failed id=%s path=%s",
                              session_id, wt_path)

        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession)
                .where(DbSession.id == session_id)
                .values(status="ended")
            )
        log.info("session.close id=%s", session_id)

    async def rename_session(self, session_id: str, name: str) -> None:
        """Update the session name in the DB and in-memory record."""
        if session_id in self._records:
            self._records[session_id].name = name
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession)
                .where(DbSession.id == session_id)
                .values(name=name)
            )

    def list_sessions(self) -> list[SessionRecord]:
        """Return all in-memory SessionRecords."""
        return list(self._records.values())

    def get_session(self, session_id: str) -> SessionRecord | None:
        """Return the in-memory SessionRecord for session_id, or None."""
        return self._records.get(session_id)

    def record(self, session_id: str) -> SessionRecord:
        """Return the in-memory SessionRecord for session_id (KeyError if missing)."""
        try:
            return self._records[session_id]
        except KeyError:
            raise KeyError(f"No session with id {session_id!r}") from None

    # ------------------------------------------------------------------
    # Event bus — WS bridge subscribes/unsubscribes for lifecycle events
    # ------------------------------------------------------------------

    def subscribe_events(self) -> "asyncio.Queue[dict]":
        """Return a queue that receives lifecycle events (rotated, sessionEnded, etc.)."""
        q: asyncio.Queue[dict] = asyncio.Queue()
        self._event_subscribers.append(q)
        return q

    def unsubscribe_events(self, q: "asyncio.Queue[dict]") -> None:
        """Remove a previously-subscribed event queue."""
        try:
            self._event_subscribers.remove(q)
        except ValueError:
            pass

    def _publish_event(self, event: dict) -> None:
        """Fan an event out to all subscribers (non-blocking)."""
        for q in list(self._event_subscribers):
            q.put_nowait(event)

    # ------------------------------------------------------------------
    # Accessors for the WS bridge
    # ------------------------------------------------------------------

    @property
    def manager(self) -> SessionManager:
        return self._manager

    @property
    def poller(self) -> UsagePoller:
        return self._poller

    # ------------------------------------------------------------------
    # Rotation monitor (background task)
    # ------------------------------------------------------------------

    async def _rotation_monitor(self) -> None:
        """Subscribe to usage events; rotate any session whose account hits ceiling."""
        q = self._poller.subscribe()
        try:
            while True:
                event = await q.get()
                account: str = event.get("account", "")
                usage = event.get("usage")
                if usage is None:
                    continue
                session_pct: float = getattr(usage, "session_pct", 0.0)
                if session_pct < _ROTATION_CEILING:
                    continue
                # Find all running sessions on this account
                for record in list(self._records.values()):
                    if record.account != account:
                        continue
                    if not self._manager.is_running(record.id):
                        continue
                    log.info(
                        "rotation.trigger session=%s account=%s pct=%.1f",
                        record.id,
                        account,
                        session_pct,
                    )
                    asyncio.create_task(
                        self._do_rotate(record),
                        name=f"rotate-{record.id}",
                    )
        except asyncio.CancelledError:
            pass
        finally:
            self._poller.unsubscribe(q)

    async def _do_rotate(self, record: SessionRecord) -> None:
        """Execute rotation for a single session and update the DB row."""
        claude_bin = self._claude_bin

        def _claude_argv(claude_session_id: str, config_dir: str) -> list[str]:
            argv = list(claude_bin)
            if claude_session_id:
                argv += ["--resume", claude_session_id]
            return argv

        deps = RotationDeps(
            manager=self._manager,
            select_account=select_account,
            compose=compose,
            claude_argv=_claude_argv,
        )
        try:
            old_account = record.account
            new_account = await rotate(record, deps)
            # Update DB
            async with db_session() as db:
                from sqlalchemy import update as sa_update
                await db.execute(
                    sa_update(DbSession)
                    .where(DbSession.id == record.id)
                    .values(account_name=new_account)
                )
            log.info(
                "rotation.complete session=%s %s→%s",
                record.id, old_account, new_account,
            )
            # Publish rotation event to WS subscribers
            self._publish_event({
                "type": "rotated",
                "sessionId": record.id,
                "fromAccount": old_account,
                "toAccount": new_account,
            })
        except AllAccountsBusy:
            log.warning("rotation.all_busy session=%s", record.id)
        except Exception:
            log.exception("rotation.error session=%s", record.id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_claude_session_id(cfg_dir: Path, cwd: str) -> str | None:
    """Best-effort: find the newest *.jsonl under cfg_dir/projects/** for cwd.

    Returns the stem (filename without .jsonl) — that is the claude session id
    used with --resume on rotation.  Returns None if not found (e.g. fake stub).
    """
    projects_dir = cfg_dir / "projects"
    if not projects_dir.exists():
        return None
    # Encode cwd as a path segment the same way claude does: replace '/' with '-'
    # and strip the leading separator.
    cwd_key = cwd.lstrip("/").replace("/", "-")
    candidate_dir = projects_dir / cwd_key
    if candidate_dir.is_dir():
        jsonl_files = sorted(candidate_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if jsonl_files:
            return jsonl_files[-1].stem
    # Fallback: scan all subdirs
    all_jsonl = sorted(
        (p for p in projects_dir.rglob("*.jsonl")),
        key=lambda p: p.stat().st_mtime,
    )
    if all_jsonl:
        return all_jsonl[-1].stem
    return None
