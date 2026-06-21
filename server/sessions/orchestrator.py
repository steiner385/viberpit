"""SessionOrchestrator — ties SessionDriver, UsagePoller, and rotation together.

The API and WS layers call this.  One instance lives on app.state.

Usage:
    orch = SessionOrchestrator(sdk_factory=real_sdk_factory)
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
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.sdk_factory import real_sdk_factory
from server.sessions.selection import AllAccountsBusy, select_account
from server.sessions.types import SessionRecord
from server.usage.poller import UsagePoller

log = logging.getLogger(__name__)


class BranchSwitchError(Exception):
    """Raised when a git branch checkout fails."""


# Session-window utilisation ceiling that triggers rotation (100 = hard ceiling per spec)
_ROTATION_CEILING: float = 100.0


class SessionOrchestrator:
    """Orchestrates session lifecycle: creation, rotation, termination.

    Args:
        sdk_factory: ``(cwd, config_dir, can_use_tool, mode, resume) -> SDKClientProtocol``
            builds the SDK client backing each SessionDriver.  Defaults to
            ``real_sdk_factory`` (a real ClaudeSDKClient); override in tests with a
            fake-SDK factory.
        work_dir: Default cwd for new sessions.  Defaults to the process cwd.
        poll_interval: Seconds between usage-poller sweeps (default 60).
        usage_fn: Override for the usage-fetch function (testing).
    """

    def __init__(
        self,
        sdk_factory=None,
        work_dir: str | None = None,
        poll_interval: float = 60.0,
        usage_fn: Callable[[str], object] | None = None,
        repo_root: str | None = None,
    ) -> None:
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
        self._sdk_factory = sdk_factory or real_sdk_factory
        self._registry = PermissionRegistry()
        self._drivers: dict[str, SessionDriver] = {}
        self._last_prompt: dict[str, str] = {}
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
        identity: str = "system",
        permission_mode: str = "interactive",
    ) -> SessionRecord:
        """Pick an account, start a SessionDriver, persist a DB row, return a SessionRecord.

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

        # Start a SessionDriver backed by the configured SDK factory.
        driver = SessionDriver(
            session_id, account, cwd, str(cfg_dir),
            mode=permission_mode, sdk_factory=self._sdk_factory,
            registry=self._registry, identity=identity,
        )
        await driver.start()
        self._drivers[session_id] = driver
        if initial_prompt:
            self._last_prompt[session_id] = initial_prompt
            await driver.send_prompt(initial_prompt)

        # Persist DB row
        async with db_session() as db:
            row = DbSession(
                id=session_id,
                name=name,
                account_name=account,
                claude_session_id=None,
                status="working" if initial_prompt else "idle",
                pid=None,
                cwd=cwd,
                permission_mode=permission_mode,
            )
            db.add(row)

        record = SessionRecord(
            id=session_id,
            name=name,
            account=account,
            claude_session_id="",
            cols=cols,
            rows=rows,
            cwd=cwd,
        )
        self._records[session_id] = record
        log.info("session.create id=%s account=%s", session_id, account)
        return record

    async def close_session(self, session_id: str) -> None:
        """Stop the driver, clean up the session worktree, mark the row ended."""
        driver = self._drivers.pop(session_id, None)
        self._last_prompt.pop(session_id, None)
        if driver is not None:
            await driver.aclose()
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
    # Accessors / delegating helpers for the WS bridge
    # ------------------------------------------------------------------

    @property
    def poller(self) -> UsagePoller:
        return self._poller

    def driver(self, session_id: str) -> SessionDriver | None:
        """Return the live SessionDriver for session_id, or None."""
        return self._drivers.get(session_id)

    def resolve_permission(self, session_id, perm_id, decision, identity) -> bool:
        d = self._drivers.get(session_id)
        return d.resolve_permission(perm_id, decision, identity) if d else False

    def answer_question(self, session_id, question_id, selected, identity) -> bool:
        """Resolve a pending AskUserQuestion with the user's selected answers.

        Returns True if resolved, False if no driver for session_id or unknown question_id.
        Mirrors resolve_permission.
        """
        d = self._drivers.get(session_id)
        return d.answer_question(question_id, selected, identity) if d else False

    async def send_prompt(self, session_id, text):
        d = self._drivers.get(session_id)
        if d:
            self._last_prompt[session_id] = text
            await d.send_prompt(text)

    async def interrupt(self, session_id):
        d = self._drivers.get(session_id)
        if d:
            await d.interrupt()

    async def set_permission_mode(self, session_id, mode):
        d = self._drivers.get(session_id)
        if d:
            await d.set_permission_mode(mode)

    async def cancel_queued(self, session_id, index: int) -> bool:
        d = self._drivers.get(session_id)
        if d:
            return await d.cancel_queued(index)
        return False

    def transcript_path(self, session_id) -> str | None:
        """<config_dir>/projects/<cwd-key>/<claude_session_id>.jsonl, or None if unknown."""
        d = self._drivers.get(session_id)
        if not d or not d.claude_session_id:
            return None
        cwd_key = d.cwd.lstrip("/").replace("/", "-")
        p = Path(d.config_dir) / "projects" / cwd_key / f"{d.claude_session_id}.jsonl"
        return str(p) if p.exists() else None

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
                    if record.id not in self._drivers:
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
        """Rotate one session to a new account, resuming the claude session inline."""
        old_driver = self._drivers.get(record.id)
        if old_driver is None:
            return
        old = record.account
        claude_sid = old_driver.claude_session_id
        try:
            await old_driver.interrupt()
            await old_driver.aclose()
            new_account = select_account(exclude=frozenset({old}))
            cfg = compose(new_account)
            configdir.trust_project(new_account, old_driver.cwd)
            new_driver = SessionDriver(
                record.id, new_account, old_driver.cwd, str(cfg),
                mode=old_driver.mode, sdk_factory=self._sdk_factory,
                registry=self._registry, identity=old_driver.identity,
                resume=claude_sid,
            )
            await new_driver.start()
            self._drivers[record.id] = new_driver
            record.account = new_account
            last = self._last_prompt.get(record.id)
            if last:
                await new_driver.send_prompt(last)
            async with db_session() as db:
                from sqlalchemy import update as sa_update
                await db.execute(
                    sa_update(DbSession)
                    .where(DbSession.id == record.id)
                    .values(account_name=new_account)
                )
            log.info("rotation.complete session=%s %s→%s", record.id, old, new_account)
            self._publish_event({
                "type": "rotated", "sessionId": record.id, "fromAccount": old,
                "toAccount": new_account, "reason": "session limit reached",
                "turnRestarted": bool(last),
            })
        except AllAccountsBusy:
            log.warning("rotation.all_busy session=%s", record.id)
        except Exception:
            log.exception("rotation.error session=%s", record.id)
