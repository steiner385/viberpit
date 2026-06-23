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
import functools
import logging
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable

from ccswitch import accounts as ccswitch_accounts
from ccswitch import configdir, worktrees
from ccswitch.account_names import assert_valid_account_name, InvalidAccountName
from ccswitch.config import load_config
from ccswitch.configdir import compose
from ccswitch.paths import shared_base
from server.audit import record_audit
from server.db import db_session
from server.db.models import Session as DbSession
from server.sessions import adopt as _adopt
from server.sessions import attachments as _attachments
from server.sessions import events as ev
from server.sessions.commands import catalog as cmd_catalog
from server.sessions.commands import dispatcher as cmd_dispatcher
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.sdk_factory import real_sdk_factory, real_suggestion_factory
from server.sessions.suggestions import SuggestionService
from server.sessions.recovery import (
    RecoveryState, RETRY_AFTER_MAX, QUARANTINE_429, QUARANTINE_401,
)
from server.sessions.selection import AllAccountsBusy, select_account
from server.sessions.title_generator import generate_title
from server.sessions.titles import read_ai_title_from_file, read_user_prompts_from_file
from server.sessions.types import SessionRecord
from server.usage.poller import UsagePoller

log = logging.getLogger(__name__)


# Statuses that mean a session is mid-turn or awaiting user input — restarting now
# would lose in-flight work. All other statuses (idle / ready_review / done /
# limited / ended / archived) are restart-safe: resume + transcript replay recover them.
_ACTIVE_STATUSES = frozenset({"in_progress", "working", "waiting_input"})


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
        suggestion_factory=None,
        work_dir: str | None = None,
        poll_interval: float = 60.0,
        usage_fn: Callable[[str], object] | None = None,
        repo_root: str | None = None,
        titling_interval: float = 15.0,
        x11=None,
        title_fn: Callable | None = None,
    ) -> None:
        # work_dir param is accepted for back-compat (used in tests to keep
        # the orchestrator anchored to the repo root), but is no longer stored
        # as an instance attribute since sessions now always resolve their own cwd.
        _ = work_dir
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
        self._x11 = x11  # X11Manager | None — per-session virtual displays
        if os.environ.get("CCSWITCH_SUGGESTIONS", "1") == "0":
            self._suggester = None
        else:
            self._suggester = SuggestionService(suggestion_factory or real_suggestion_factory)
        # Cadence (seconds) of the background titling sweep that adopts Claude's
        # own transcript title into still-auto-named sessions. Constructor arg so
        # tests can drive it fast.
        self._titling_interval: float = titling_interval
        # Fallback title generator: Claude only writes an ``ai-title`` record in
        # the interactive CLI, so SDK-driven sessions get none. When the transcript
        # has no title, summarize the user's own prompts with a one-shot Haiku query.
        # Injectable so tests can stub it without a live SDK call.
        self._title_fn: Callable = title_fn or self._default_title_fn
        self._registry = PermissionRegistry()
        self._drivers: dict[str, SessionDriver] = {}
        self._last_prompt: dict[str, str] = {}
        # Failure-driven recovery: quarantine + per-session rotation cap. Clock is
        # injectable for deterministic tests; production uses wall-clock time. The
        # recompose-attempt map distinguishes a first 401 (heal in place) from a
        # repeat 401 (creds genuinely bad → rotate away).
        self._clock: Callable[[], float] = time.time
        self._recovery = RecoveryState()
        self._recompose_attempts: dict[str, float] = {}  # session_id → last recompose ts
        self._recompose_ttl: float = 60.0
        # initial_prompt deferred until first WS attach so the client is watching
        # when the turn runs — avoids losing events before any consumer exists.
        self._pending_initial_prompt: dict[str, str] = {}
        # Deferred attachment ids for the initial prompt, sent on first WS attach.
        self._pending_initial_attachment_ids: dict[str, list[str]] = {}

        # Build a live-session-aware usage_fn: when an account has a live session
        # the SDK owns its token family, so the poller must not independently
        # refresh.  Pass allow_refresh=False for those accounts so the poller
        # calls sync_back first and returns None on an expired-but-owned token
        # (the SDK will refresh on its next request).
        if usage_fn is not None:
            _poller_usage_fn = usage_fn
        else:
            def _poller_usage_fn(name: str):
                return ccswitch_accounts.usage_for(
                    name,
                    allow_refresh=not self.has_live_session(name),
                )

        self._poller: UsagePoller = UsagePoller(
            poll_interval=poll_interval,
            usage_fn=_poller_usage_fn,
        )
        # In-memory map: session_id → SessionRecord
        self._records: dict[str, SessionRecord] = {}
        # Background tasks
        self._rotation_task: asyncio.Task | None = None
        self._titling_task: asyncio.Task | None = None
        # Event bus — WS bridge subscribes here for lifecycle events
        self._event_subscribers: list[asyncio.Queue] = []
        # Serialises the check-build-register critical section in ensure_driver
        # so that two concurrent WS attaches to the same detached session cannot
        # each see no driver, each build+start one, and both register → two SDK
        # processes attached to the same Claude conversation (TOCTOU fix).
        self._ensure_lock: asyncio.Lock = asyncio.Lock()

    def _session_factory(self, session_id: str):
        """Return a 5-arg SDK factory closure injecting this session's DISPLAY.

        When no X11 display is allocated for this session, the closure calls the
        base factory with only the standard 5 positional arguments so that test
        factories (which don't accept ``extra_env``) keep working unchanged.
        """
        base = self._sdk_factory
        x11 = self._x11
        def _factory(cwd, config_dir, can_use_tool, mode, resume):
            extra = x11.env_for(session_id) if x11 else {}
            if extra:
                return base(cwd, config_dir, can_use_tool, mode, resume, extra_env=extra)
            return base(cwd, config_dir, can_use_tool, mode, resume)
        return _factory

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the usage poller, the rotation monitor and the titling sweep."""
        await self._poller.start()
        self._rotation_task = asyncio.create_task(
            self._rotation_monitor(), name="rotation-monitor"
        )
        self._titling_task = asyncio.create_task(
            self._titling_monitor(), name="titling-monitor"
        )

    async def stop(self) -> None:
        """Stop the titling sweep, rotation monitor and usage poller."""
        for attr in ("_titling_task", "_rotation_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)
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
        repo: str | None = None,
        identity: str = "system",
        permission_mode: str = "interactive",
        attachment_ids: list[str] | None = None,
        name_is_auto: bool = True,
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

        session_id = str(uuid.uuid4())

        # Per-session isolation: EVERY repo session runs in its OWN git worktree, never
        # the shared clone or the user's real tree. With a branch, the worktree checks it
        # out (child-branch fallback if it's busy elsewhere); without a branch, it's a
        # detached worktree off HEAD. Either way two sessions on one repo never collide,
        # and the user's real checkout is untouched. (No-repo sessions get a scratch dir
        # below; this branch only applies when cwd is an actual git repo — the ccswitch
        # clone resolved by the API.)
        worktree_info: worktrees.WorktreeInfo | None = None
        if cwd is not None and worktrees.is_git_repo(cwd):
            try:
                worktree_info = await asyncio.to_thread(
                    worktrees.create_worktree, cwd, branch, session_id, self._repo_root
                )
            except worktrees.WorktreeError as exc:
                raise BranchSwitchError(str(exc)) from exc
            assert worktree_info is not None  # create_worktree returns a value or raises
            cwd = worktree_info.path
            self._worktrees[session_id] = worktree_info

        # Resolve final cwd: an explicit cwd was already validated above; a
        # branch-selected session already has cwd=worktree path.  For a no-repo
        # session (no explicit cwd, no worktree) we create an isolated scratch dir
        # under the ccswitch base so the agent never runs in the server's own
        # checkout.  This is the safety guarantee stated in the New Session dialog:
        # "your real checkouts are never touched."
        if cwd is None:
            scratch_dir = Path(worktrees.worktrees_base(self._repo_root)).parent / "scratch" / session_id
            scratch_dir.mkdir(parents=True, exist_ok=True)
            cwd = str(scratch_dir)

        # Branch this session's worktree is on (None for detached/scratch) — the
        # join key to its GitHub PR. Surfaced as a PR pill on the dashboard.
        branch_name = worktree_info.branch if worktree_info is not None else None

        if account is None:
            # Skip accounts currently quarantined for a 429/401 so a fresh session
            # doesn't land on an account we already know is struggling.
            account = select_account(
                exclude=self._recovery.quarantined(now=self._clock()),
                load=self.live_session_load())
        else:
            # Verify the given account exists (will raise if cred file missing — acceptable)
            pass

        cfg_dir: Path = compose(account)

        # Pre-trust the project directory (now the worktree, if any) so claude
        # won't prompt for trust on startup.
        configdir.trust_project(account, cwd)

        # Allocate a virtual X11 display for this session (if X11Manager wired in).
        if self._x11 is not None:
            try:
                await self._x11.start_display(session_id)
            except Exception:
                log.exception("session.display_start_failed id=%s", session_id)

        # Start a SessionDriver backed by the configured SDK factory.
        driver = SessionDriver(
            session_id, account, cwd, str(cfg_dir),
            mode=permission_mode, sdk_factory=self._session_factory(session_id),
            registry=self._registry, identity=identity,
            on_session_id=self._persist_claude_session_id,
            on_sync_back=functools.partial(ccswitch_accounts.sync_back, account),
            on_status=self._persist_status,
            on_recoverable_failure=self._handle_recoverable_failure,
            suggester=self._suggester,
        )
        await driver.start()
        self._drivers[session_id] = driver
        if initial_prompt:
            # Defer sending the initial prompt until the first WebSocket client
            # attaches.  Sending it here (before any consumer exists) causes the
            # turn's events to accumulate in driver._out with no guarantee they
            # will be visible: the transcript file may not be flushed yet, and
            # buffered _out events can race the client's replay.  Deferring means
            # the client is already watching when the turn runs — events stream
            # live, so nothing is lost.
            self._pending_initial_prompt[session_id] = initial_prompt
        if attachment_ids:
            self._pending_initial_attachment_ids[session_id] = list(attachment_ids)

        # Persist DB row
        async with db_session() as db:
            row = DbSession(
                id=session_id,
                name=name,
                account_name=account,
                claude_session_id=None,
                status="idle",
                pid=None,
                cwd=cwd,
                repo=repo,
                branch=branch_name,
                permission_mode=permission_mode,
                name_is_auto=name_is_auto,
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
            repo=repo,
            branch=branch_name,
            status="idle",
            name_is_auto=name_is_auto,
        )
        self._records[session_id] = record
        log.info("session.create id=%s account=%s", session_id, account)
        return record

    async def close_session(self, session_id: str) -> None:
        """Stop the driver, clean up the session worktree, mark the row ended."""
        # Teardown the virtual X11 display — runs unconditionally so orphaned
        # displays are never leaked even when the driver was never registered.
        if self._x11 is not None:
            try:
                await self._x11.stop_display(session_id)
            except Exception:
                log.exception("session.display_cleanup_failed id=%s", session_id)

        driver = self._drivers.pop(session_id, None)
        self._last_prompt.pop(session_id, None)
        self._pending_initial_prompt.pop(session_id, None)
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

        # Remove the no-repo scratch dir if this session used one.  Guard with a
        # strict containment check so we never rm anything outside the scratch base.
        if record and record.cwd:
            scratch_base = Path(worktrees.worktrees_base(self._repo_root)).parent / "scratch"
            try:
                cwd_resolved = Path(record.cwd).resolve()
                scratch_base_resolved = scratch_base.resolve()
                cwd_resolved.relative_to(scratch_base_resolved)  # raises ValueError if not under base
                if cwd_resolved.is_dir():
                    import shutil
                    await asyncio.to_thread(shutil.rmtree, str(cwd_resolved))
                    log.info("session.scratch_cleanup id=%s path=%s", session_id, cwd_resolved)
            except ValueError:
                pass  # Not a scratch dir — nothing to do
            except Exception:
                log.exception("session.scratch_cleanup_failed id=%s", session_id)

        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession)
                .where(DbSession.id == session_id)
                .values(status="ended")
            )
        log.info("session.close id=%s", session_id)

    async def rename_session(self, session_id: str, name: str) -> None:
        """Update the session name in the DB and in-memory record.

        A manual rename pins ``name_is_auto = False`` so auto-naming never overwrites it.
        """
        if session_id in self._records:
            self._records[session_id].name = name
            self._records[session_id].name_is_auto = False
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession)
                .where(DbSession.id == session_id)
                .values(name=name, name_is_auto=False)
            )

    async def _persist_status(self, session_id: str, status: str) -> None:
        """Driver ``on_status`` callback: persist the derived dashboard status.

        Updates the in-memory record (which the dashboard's 5s poll reads) and the DB
        row. Never downgrades a terminal ``ended`` status. When a turn finishes
        (``done``), opportunistically refresh the auto-name from Claude's title.
        """
        rec = self._records.get(session_id)
        if rec is not None and rec.status == "ended":
            return
        if rec is not None:
            rec.status = status
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id).values(status=status)
            )
        # Refresh the per-sub-agent summary so the dashboard list can nest sub-agents
        # under this session. Best-effort: a failure must never break the session loop.
        await self._persist_sub_agents(session_id)
        # Live-push the change to every connected dashboard so the session LIST updates
        # immediately, instead of waiting for the 5s poll (which the browser suspends
        # while the ccswitch tab is backgrounded — the cause of cards getting stuck on
        # "Working…" until the session is opened). Mirrors the "rotated" broadcast; the
        # WS bridge forwards lifecycle frames top-level so the client's status handler
        # (_patchSessionStatus) patches the cached session record.
        self._publish_event({"type": "status", "sessionId": session_id, "status": status})
        if status == "done":
            await self._maybe_apply_auto_name(session_id)

    async def _persist_sub_agents(self, session_id: str) -> None:
        """Recompute the session's sub-agent summary from the transcript and persist
        it to the in-memory record + DB row. Best-effort; swallows all errors."""
        try:
            from .subagents import summarize_transcript
            events = await self.transcript_events(session_id)
            summary = summarize_transcript(events)
            rec = self._records.get(session_id)
            if rec is not None:
                rec.sub_agents = summary
            async with db_session() as db:
                from sqlalchemy import update as sa_update
                await db.execute(
                    sa_update(DbSession).where(DbSession.id == session_id).values(sub_agents=summary)
                )
        except Exception:
            log.debug("persist_sub_agents failed (non-fatal) session=%s", session_id, exc_info=True)

    async def _default_title_fn(self, prompts, *, account, cwd):
        """Default fallback title generator: a one-shot Haiku query under the account."""
        return await generate_title(prompts, config_dir=str(compose(account)), cwd=cwd)

    def _account_limited(self, account: str) -> bool:
        """True if *account*'s last-known session usage is at/above the ceiling.

        Best-effort: an unknown account (no cached usage) is treated as not
        limited — a doomed title query would just degrade silently anyway.
        """
        usage = self._poller.cached(account)
        return usage is not None and getattr(usage, "session_pct", 0.0) >= _ROTATION_CEILING

    async def _generate_fallback_title(self, rec: SessionRecord, path: str) -> str | None:
        """Summarize the session's user prompts into a title, or ``None``.

        Used when Claude wrote no ``ai-title`` (the common case for SDK-driven
        sessions). Skips accounts at their usage ceiling and sessions with no
        readable prompts. Best-effort: any miss leaves the name unchanged.
        """
        if self._account_limited(rec.account):
            return None
        prompts = await asyncio.to_thread(read_user_prompts_from_file, path)
        if not prompts:
            return None
        return await self._title_fn(prompts, account=rec.account, cwd=rec.cwd)

    async def _maybe_apply_auto_name(self, session_id: str) -> None:
        """If the session name is still auto, adopt a title for it.

        Prefers Claude's own transcript ``ai-title``; when absent (SDK-driven
        sessions never get one) falls back to summarizing the user prompts. Updates
        the DB + in-memory record and, when a live driver exists, pushes a ``Title``
        event so the in-session header converges with the dashboard. Idempotent: a
        title equal to the current name is a no-op.
        """
        rec = self._records.get(session_id)
        if rec is None or not rec.name_is_auto:
            return
        path = self.transcript_path(session_id)
        if not path:
            return
        title = await asyncio.to_thread(read_ai_title_from_file, path)
        source = "claude"
        if not title:
            title = await self._generate_fallback_title(rec, path)
            source = "generated"
        rec = self._records.get(session_id)
        if rec is None or not rec.name_is_auto or not title or title == rec.name:
            return
        rec.name = title
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id).values(name=title)
            )
        driver = self._drivers.get(session_id)
        if driver is not None:
            await driver._out.put(ev.Title(title=title, source=source))
        log.info("session.auto_name id=%s title=%r source=%s", session_id, title, source)

    async def _titling_sweep(self) -> None:
        """One titling pass: refresh the auto-name of every active, still-auto session.

        The turn-boundary trigger ("done") handles the common case; this catches titles
        Claude writes after the final turn or slowly mid-turn. Never raises.
        """
        for sid, rec in list(self._records.items()):
            if rec.status == "ended" or not rec.name_is_auto:
                continue
            try:
                await self._maybe_apply_auto_name(sid)
            except Exception:  # never let one session break the sweep
                log.warning("titling sweep failed for %s", sid, exc_info=True)

    async def _titling_monitor(self) -> None:
        """Background loop: sweep titles every ``self._titling_interval`` seconds."""
        while True:
            await self._titling_sweep()
            await asyncio.sleep(self._titling_interval)

    async def _persist_claude_session_id(self, session_id: str, claude_session_id: str) -> None:
        """Callback fired by the driver the first time it captures the Claude session id.

        Keeps the in-memory record in sync and immediately persists to the DB so the id
        survives a restart without waiting for rotation.
        """
        rec = self._records.get(session_id)
        if rec is not None:
            rec.claude_session_id = claude_session_id
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession)
                .where(DbSession.id == session_id)
                .values(claude_session_id=claude_session_id)
            )

    async def rehydrate(self) -> int:
        """Rebuild detached SessionRecords from non-ended DB rows (lazy: no drivers).

        Idempotent: skips ids already present in _records. Normalizes a pre-crash
        'working' status to 'idle' (the process is gone). Returns the count of rows added.
        """
        from sqlalchemy import select as sa_select
        async with db_session() as db:
            rows = (await db.execute(
                sa_select(DbSession).where(
                DbSession.status.notin_(["ended", "archived"])
            )
            )).scalars().all()
        added = 0
        for row in rows:
            if row.id in self._records:
                continue
            # A pre-crash live status has no driver anymore → normalize to idle.
            status = "idle" if row.status in ("working", "in_progress") else row.status
            self._records[row.id] = SessionRecord(
                id=row.id,
                name=row.name,
                account=row.account_name,
                claude_session_id=row.claude_session_id or "",
                cols=80,
                rows=24,
                cwd=row.cwd,
                repo=row.repo,
                detached=True,
                status=status,
                name_is_auto=row.name_is_auto,
                permission_mode=row.permission_mode,
                created_at=row.created_at,
                adopted_in_place=bool(getattr(row, "adopted_in_place", False)),
                model=getattr(row, "model", None),
                account_pinned=bool(getattr(row, "account_pinned", False)),
            )
            added += 1
        log.info("rehydrate: %d session(s) restored", added)
        return added

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

    def active_session_count(self) -> int:
        """Number of sessions currently mid-turn or awaiting input (restart-blocking)."""
        return sum(1 for r in self._records.values() if r.status in _ACTIVE_STATUSES)

    def restart_safe(self) -> bool:
        """True when a restart now would interrupt no in-flight work."""
        return self.active_session_count() == 0

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

    def has_live_session(self, account: str) -> bool:
        """Return True if any live SessionDriver is currently running on *account*.

        Used by the poller's usage_fn to decide whether it is safe to call
        oauth.refresh_access_token: while the SDK subprocess owns the token
        family (live session), only the SDK must refresh so we avoid
        single-use-token reuse-detection.
        """
        # Snapshot values() before iterating: _drivers is mutated by the event
        # loop (create_session / close_session / _do_rotate) while this method
        # can be called from a threadpool thread (the poller's usage_fn closure).
        # Iterating the live dict while the event loop mutates it raises
        # RuntimeError: dictionary changed size during iteration.
        return any(d.account == account for d in list(self._drivers.values()))

    def live_session_load(self) -> dict[str, float]:
        """Weighted count of live sessions per account, for load-aware selection.

        A live session is one with an entry in ``_drivers`` (same liveness notion as
        ``has_live_session``). Actively-working sessions (_ACTIVE_STATUSES) weigh 1.0;
        idle live sessions weigh ``cfg.idle_session_weight``. Accounts with no live
        session are absent from the map (treated as load 0 by the selector).

        Snapshots ``_drivers`` before iterating — the event loop mutates it while this
        may run from a threadpool thread (see ``has_live_session``).
        """
        cfg = load_config()
        load: dict[str, float] = {}
        for sid in list(self._drivers.keys()):
            rec = self._records.get(sid)
            if rec is None:
                continue
            weight = 1.0 if rec.status in _ACTIVE_STATUSES else cfg.idle_session_weight
            load[rec.account] = load.get(rec.account, 0.0) + weight
        return load

    def driver(self, session_id: str) -> SessionDriver | None:
        """Return the live SessionDriver for session_id, or None."""
        return self._drivers.get(session_id)

    async def ensure_driver(self, session_id: str, identity: str = "system") -> SessionDriver | None:
        """Return the live SessionDriver for session_id, resuming from DB if detached.

        - If a live driver already exists, return it (idempotent).
        - If the SessionRecord exists but is detached, build a new SessionDriver with
          resume=<claude_session_id>, start it, store it, and mark the record as live.
        - If the session_id is unknown, return None.

        ``identity`` is the verified identity of the caller attaching to the session
        (the WebSocket connection's Tailscale identity). It MUST be threaded onto the
        resumed driver: AskUserQuestion / permission answers are registered under
        ``driver.identity`` and later resolved with the WS identity, so a resumed
        driver built with the wrong identity rejects every answer (silently dropping
        them and hanging the agent). Defaults to "system" for internal callers.
        """
        # Fast path: live driver already exists (no lock needed — read-only check)
        existing = self._drivers.get(session_id)
        if existing is not None:
            return existing

        # Look up the in-memory record (populated by rehydrate())
        rec = self._records.get(session_id)
        if rec is None:
            return None

        # Serialise the check-build-register section so that two concurrent WS
        # attaches to the same detached session cannot both pass the "no driver"
        # gate, each build+start a driver, and both register — which would attach
        # two SDK processes to the same Claude conversation (TOCTOU fix).
        async with self._ensure_lock:
            # Re-check inside the lock: another coroutine may have built the
            # driver while we were waiting.
            existing = self._drivers.get(session_id)
            if existing is not None:
                return existing

            # Build and start a new driver resuming from the stored claude_session_id
            resume = rec.claude_session_id or None
            cfg_dir = compose(rec.account)
            # Ensure the working directory exists. A rehydrated session's cwd (a no-repo
            # scratch dir or a git worktree) may have been removed when the session was
            # previously closed (close_session cleans those up); without recreating it the
            # SDK fails with "Working directory does not exist", which surfaced as a
            # misleading "account needs re-authentication" error.
            Path(rec.cwd).mkdir(parents=True, exist_ok=True)
            configdir.trust_project(rec.account, rec.cwd)

            def _build(resume_arg):
                return SessionDriver(
                    session_id,
                    rec.account,
                    rec.cwd,
                    str(cfg_dir),
                    mode=getattr(rec, "permission_mode", "interactive"),
                    sdk_factory=self._session_factory(session_id),
                    registry=self._registry,
                    identity=identity,
                    resume=resume_arg,
                    on_session_id=self._persist_claude_session_id,
                    on_sync_back=functools.partial(ccswitch_accounts.sync_back, rec.account),
                    on_status=self._persist_status,
                    on_recoverable_failure=self._handle_recoverable_failure,
                    suggester=self._suggester,
                )

            driver = _build(resume)
            try:
                await driver.start()
            except Exception:
                if resume is None:
                    # No resume to blame — the SDK genuinely can't start (e.g. a
                    # truly dead account token / system problem). Surface it.
                    raise
                # Resume failed — almost always because the stored claude_session_id
                # can't be resumed from rec.cwd (the original session lived under a
                # different project dir, or the claude session was pruned). Fall back
                # to a FRESH session so the record stays usable: the prior transcript
                # still shows via replay, and new turns get (and persist) a new
                # claude_session_id. Only if the fresh start ALSO fails do we error.
                log.warning("session.resume id=%s could not resume claude_sid=%s; starting fresh",
                            session_id, resume, exc_info=True)
                try:
                    await driver.aclose()
                except Exception:
                    pass
                driver = _build(None)
                await driver.start()
            self._drivers[session_id] = driver
            rec.detached = False
            log.info("session.resume id=%s resume=%s", session_id, resume or "<fresh>")
            return driver

    # ------------------------------------------------------------------
    # Adoption — bring an untracked live claude session under management
    # ------------------------------------------------------------------

    def _resolve_candidate(self, claude_session_id: str):
        """Re-run discovery and return the AdoptionCandidate for this sid, or None.

        Re-resolving (rather than trusting a stale list) is deliberate: processes
        churn fast, so the candidate may have died or changed between the dashboard
        scan and the adopt call. Anchored to the transcript, which persists.
        """
        known = [a["name"] for a in ccswitch_accounts.list_accounts()]
        tracked = {r.claude_session_id for r in self._records.values() if r.claude_session_id}
        for cand in _adopt.discover_candidates(known, tracked):
            if cand.claude_session_id == claude_session_id:
                return cand
        return None

    async def adopt_session(self, claude_session_id: str, identity: str, killer=os.kill):
        """Force-take-over an untracked live session: terminate it, resume in place.

        Returns ``(SessionRecord, {"resumed": bool, "terminatedPid": int|None})``.
        Raises KeyError if the session is no longer adoptable (gone, unmapped, or
        already tracked). The conversation is resumed in its ORIGINAL cwd with no
        worktree (documented isolation exception, flagged adopted_in_place=True).
        """
        cand = self._resolve_candidate(claude_session_id)
        if cand is None or not cand.mapped or cand.tracked:
            raise KeyError(claude_session_id)

        # Terminate the original process tree (best-effort; a dead PID is fine).
        terminated_pid: int | None = None
        if cand.pid is not None:
            sent = await asyncio.to_thread(_adopt.terminate_process_tree, cand.pid, killer)
            if sent:
                terminated_pid = cand.pid
                async with db_session() as db:
                    await record_audit(
                        db, "process.terminate", identity,
                        {"pid": cand.pid, "claudeSessionId": claude_session_id},
                    )

        session_id = str(uuid.uuid4())
        cfg_dir = compose(cand.account)
        Path(cand.cwd).mkdir(parents=True, exist_ok=True)
        configdir.trust_project(cand.account, cand.cwd)

        # Allocate a virtual X11 display for the adopted session (fresh allocation).
        if self._x11 is not None:
            try:
                await self._x11.start_display(session_id)
            except Exception:
                log.exception("session.display_start_failed id=%s", session_id)

        def _build(resume_arg):
            return SessionDriver(
                session_id, cand.account, cand.cwd, str(cfg_dir),
                mode="interactive", sdk_factory=self._session_factory(session_id), registry=self._registry,
                identity=identity, resume=resume_arg,
                on_session_id=self._persist_claude_session_id,
                on_sync_back=functools.partial(ccswitch_accounts.sync_back, cand.account),
                on_status=self._persist_status,
                on_recoverable_failure=self._handle_recoverable_failure,
                suggester=self._suggester,
            )

        # Resume the conversation; fall back to a fresh session if resume fails so
        # the adopted record stays usable (prior transcript still shows via replay).
        resumed = True
        driver = _build(claude_session_id)
        try:
            await driver.start()
        except Exception:
            log.warning("adopt.resume_failed sid=%s; starting fresh",
                        claude_session_id, exc_info=True)
            try:
                await driver.aclose()
            except Exception:
                pass
            driver = _build(None)
            await driver.start()
            resumed = False
        self._drivers[session_id] = driver

        async with db_session() as db:
            db.add(DbSession(
                id=session_id, name=cand.name, account_name=cand.account,
                claude_session_id=claude_session_id, status="idle", pid=None,
                cwd=cand.cwd, repo=None, permission_mode="interactive",
                name_is_auto=True, adopted_in_place=True,
            ))
            await record_audit(
                db, "session.adopt", identity,
                {"sessionId": session_id, "account": cand.account,
                 "claudeSessionId": claude_session_id, "resumed": resumed},
            )

        rec = SessionRecord(
            id=session_id, name=cand.name, account=cand.account,
            claude_session_id=claude_session_id, cols=80, rows=24,
            cwd=cand.cwd, repo=None, status="idle", adopted_in_place=True,
        )
        self._records[session_id] = rec
        log.info("session.adopt id=%s sid=%s resumed=%s killed=%s",
                 session_id, claude_session_id, resumed, terminated_pid)
        return rec, {"resumed": resumed, "terminatedPid": terminated_pid}

    def resolve_permission(self, session_id, perm_id, decision, identity) -> bool:
        d = self._drivers.get(session_id)
        return d.resolve_permission(perm_id, decision, identity) if d else False

    def answer_question(self, session_id, question_id, selected, identity, mode="option") -> bool:
        """Resolve a pending AskUserQuestion with the user's selected answers and mode.

        Returns True if resolved, False if no driver for session_id or unknown question_id.
        Mirrors resolve_permission.
        """
        d = self._drivers.get(session_id)
        return d.answer_question(question_id, selected, identity, mode) if d else False

    def pending_questions(self, session_id: str) -> list[dict]:
        """Return still-pending question payloads for session_id ([] if no driver).

        Each entry has: question_id, turn_id, header, question, options, multi_select.
        Mirrors the _registry.pending(session_id) pattern used for permissions in _handle_attach.
        """
        d = self._drivers.get(session_id)
        return d.pending_questions() if d else []

    def pop_pending_initial_prompt(self, session_id: str) -> str | None:
        """Return and remove the deferred initial prompt for session_id, or None.

        Called by the WS bridge on the first attach so the initial prompt is sent
        while a client is already watching.  Clears the entry so subsequent attaches
        (reconnects) do not re-send it.
        """
        return self._pending_initial_prompt.pop(session_id, None)

    def pop_pending_initial_attachment_ids(self, session_id: str) -> list[str] | None:
        """Return and remove the deferred initial attachment ids for session_id, or None.

        Called by the WS bridge on the first attach alongside pop_pending_initial_prompt.
        Clears the entry so subsequent attaches (reconnects) do not re-send them.
        """
        return self._pending_initial_attachment_ids.pop(session_id, None)

    async def send_prompt(self, session_id, text, attachment_ids=None):
        d = self._drivers.get(session_id)
        if not d:
            return
        ids = list(attachment_ids or [])
        if len(ids) > _attachments.MAX_PER_MESSAGE:
            log.warning("send_prompt: capping attachments from %d to %d session=%s",
                        len(ids), _attachments.MAX_PER_MESSAGE, session_id)
        resolved: list[dict] = []
        if ids:
            # Bind rows to this session + a fresh turn id, then resolve the
            # content-addressed on-disk paths the driver loads into image blocks.
            turn_id = uuid.uuid4().hex
            async with db_session() as db:
                resolved = await _attachments.resolve_and_bind(
                    db, session_id, turn_id, ids)
        self._last_prompt[session_id] = text
        await d.send_prompt(text, resolved)

    async def interrupt(self, session_id):
        d = self._drivers.get(session_id)
        if d:
            await d.interrupt()

    async def set_permission_mode(self, session_id, mode):
        d = self._drivers.get(session_id)
        if d:
            await d.set_permission_mode(mode)

    async def command_catalog(self, session_id) -> list[dict]:
        """Build the per-session slash-command catalog (wire dicts). [] if no driver."""
        d = self._drivers.get(session_id)
        if d is None:
            return []
        try:
            info = await d.server_info()
        except Exception:
            log.warning("command_catalog: server_info failed session=%s", session_id, exc_info=True)
            return []
        return [c.to_wire() for c in cmd_catalog.build_catalog(info)]

    async def run_command(self, session_id, name, args) -> dict | None:
        """Dispatch a slash command. Raises KeyError if the command is unknown."""
        d = self._drivers.get(session_id)
        if d is None:
            raise KeyError(name)
        if str(name).lstrip("/") == "model":
            arg = (args or "").strip()
            await self.set_model_for_session(session_id, arg or None)
            return {"name": "model", "render": "notice",
                    "data": {"text": f"Model → {arg or 'default'}"}}
        if str(name).lstrip("/") == "account":
            acct = (args or "").strip()
            if not acct:
                return {"name": "account", "render": "notice",
                        "data": {"text": "Usage: /account <account>"}}
            try:
                await self.switch_account_manual(session_id, acct)
            except (InvalidAccountName, KeyError):
                return {"name": "account", "render": "notice",
                        "data": {"text": f"Unknown account: {acct}"}}
            return {"name": "account", "render": "notice",
                    "data": {"text": f"Account → {acct}"}}
        info = await d.server_info()
        by_name = {c.name: c for c in cmd_catalog.build_catalog(info)}
        cmd = by_name.get(str(name).lstrip("/"))
        if cmd is None:
            raise KeyError(name)
        return await cmd_dispatcher.dispatch(d, cmd, args or "")

    async def cancel_queued(self, session_id, index: int) -> bool:
        d = self._drivers.get(session_id)
        if d:
            return await d.cancel_queued(index)
        return False

    def transcript_path(self, session_id) -> str | None:
        """Locate the Claude transcript `<config_dir>/projects/*/<claude_session_id>.jsonl`.

        The transcript is found by GLOBBING for its globally-unique claude_session_id
        across all project dirs, rather than computing the project-dir name from cwd.
        Claude derives project-dir names by replacing BOTH '/' and '.' with '-' and
        KEEPS the leading slash (→ leading dash) — replicating that exactly is fragile
        (the old cwd.lstrip("/").replace("/","-") got the leading dash wrong, so replay
        found nothing → blank history for every session). Globbing by the unique UUID is
        robust to the naming convention AND to cwd/project mismatches (a session whose
        recorded cwd differs from where it actually ran still resolves).

        Falls back to the SessionRecord + compose(account) when the driver is absent
        or has no claude_session_id (e.g. rehydrated/detached sessions).

        Each account's per-account `projects` dir is itself a symlink to the one shared
        projects tree (configdir.SHARE_ALLOWLIST). That symlink can rot — point at itself
        or a sibling account — which makes the glob through `<config_dir>/projects` resolve
        nothing and silently blanks the transcript on attach (idle "Waiting for input"
        sessions only show replayed history, so they go blank; a live session masks it by
        streaming). Since the claude_session_id is globally unique and every transcript
        physically lives under the shared base, we also search shared_base()/projects so
        replay survives a broken per-account symlink.
        """
        import glob as _glob

        d = self._drivers.get(session_id)
        if d and d.claude_session_id:
            config_dir, csid = d.config_dir, d.claude_session_id
        else:
            rec = self._records.get(session_id)
            if rec is None or not rec.claude_session_id:
                return None
            config_dir, csid = str(compose(rec.account)), rec.claude_session_id

        # Probe the per-account config dir first (the common, healthy case), then the
        # canonical shared base as a backstop against a broken per-account symlink.
        # Dedup by resolved path so a healthy symlink isn't globbed twice.
        seen: set[str] = set()
        for root in (Path(config_dir), Path(shared_base())):
            projects = root / "projects"
            key = str(projects.resolve()) if projects.exists() else str(projects)
            if key in seen:
                continue
            seen.add(key)
            matches = _glob.glob(str(projects / "*" / f"{csid}.jsonl"))
            if matches:
                return matches[0]
        return None

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
                    if record.account_pinned:
                        log.info("rotation.skip_pinned session=%s account=%s", record.id, account)
                        record.status = "limited"
                        asyncio.create_task(
                            self._persist_status(record.id, "limited"),
                            name=f"limited-{record.id}",
                        )
                        self._publish_event({
                            "type": "status", "sessionId": record.id, "state": "limited",
                        })
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
        """Usage-ceiling rotation entrypoint (called by the poller monitor)."""
        await self._rotate_away(record, reason="session limit reached")

    # ------------------------------------------------------------------
    # Failure-driven recovery (429 / 401 during a live turn)
    # ------------------------------------------------------------------

    async def _handle_recoverable_failure(self, session_id: str, kind: str,
                                          retry_after: float | None) -> None:
        """Driver callback: a turn failed with a recoverable kind. Recover off the
        event loop so the driver's drain is never blocked."""
        record = self._records.get(session_id)
        if record is None or session_id not in self._drivers:
            return
        asyncio.create_task(
            self._recover_session(record, kind, retry_after),
            name=f"recover-{session_id}",
        )

    async def _recover_session(self, record: SessionRecord, kind: str,
                               retry_after: float | None) -> None:
        """Apply the recovery policy for one recoverable turn failure."""
        now = self._clock()
        if self._recovery.rotation_cap_reached(record.id, now=now):
            await self._pause_session(record, reason="rotation_cap")
            return
        if kind == "rate_limit":
            if retry_after is not None and retry_after <= RETRY_AFTER_MAX:
                await self._replay_same_account(record, retry_after)
                return
            cooldown = retry_after if (retry_after and retry_after > 0) else QUARANTINE_429
            self._recovery.quarantine(record.account, until=now + cooldown)
            await self._rotate_away(record, reason="rate_limit")
            return
        if kind == "auth_401":
            if not self._recently_recomposed(record.id, now):
                self._recompose_attempts[record.id] = now
                await self._recompose_same_account(record)
                return
            # A 401 that survives a recompose means the stored creds are genuinely
            # bad — quarantine, propagate to the ledger (observability), then rotate.
            self._recompose_attempts.pop(record.id, None)
            self._recovery.quarantine(record.account, until=now + QUARANTINE_401)
            self._mark_needs_reauth(record.account)
            await self._rotate_away(record, reason="auth_401")
            return

    def _recently_recomposed(self, session_id: str, now: float) -> bool:
        ts = self._recompose_attempts.get(session_id)
        return ts is not None and (now - ts) <= self._recompose_ttl

    async def _replay_same_account(self, record: SessionRecord, retry_after: float) -> None:
        """Wait out a short Retry-After, then re-send the last prompt on the same account."""
        await asyncio.sleep(max(0.0, retry_after))
        driver = self._drivers.get(record.id)
        last = self._last_prompt.get(record.id)
        if driver is not None and last:
            await driver.send_prompt(last)

    async def _recompose_same_account(self, record: SessionRecord) -> None:
        """Heal a clobbered live .credentials.json: recompose from the store and relaunch
        the same account in place (a running claude holds the bad creds in memory)."""
        await self._reassign(record, record.account, reason="recompose")

    async def _rotate_away(self, record: SessionRecord, *, reason: str) -> None:
        """Select a healthy account (excluding quarantined + current) and reassign."""
        now = self._clock()
        old = record.account
        exclude = self._recovery.quarantined(now=now) | frozenset({old})
        try:
            new_account = select_account(exclude=exclude, load=self.live_session_load())
        except AllAccountsBusy as e:
            await self._pause_session(record, reason="all_accounts_busy",
                                      earliest_reset=e.earliest_reset)
            return
        if record.account_pinned:
            record.account_pinned = False
            await self._persist_pin(record.id, False)
        self._recovery.record_rotation(record.id, now=now)
        await self._reassign(record, new_account, reason=reason)

    async def _pause_session(self, record: SessionRecord, *, reason: str,
                             earliest_reset=None) -> None:
        """No recovery possible (no healthy account / rotation cap). Flag the session
        for the user and publish a machine-readable paused event."""
        try:
            await self._persist_status(record.id, "needs_attention")
        except Exception:
            log.warning("pause.persist_status failed session=%s", record.id, exc_info=True)
        self._publish_event({
            "type": "paused", "sessionId": record.id, "account": record.account,
            "reason": reason,
            "earliestReset": earliest_reset.isoformat() if earliest_reset else None,
        })
        log.warning("recover.paused session=%s reason=%s", record.id, reason)

    def _mark_needs_reauth(self, account: str) -> None:
        """Propagate a recompose-failed in-session 401 to the ledger so the dashboard
        stops reporting a falsely-healthy 'ok' for an account whose creds are dead.
        A transient 429 never reaches here — only a 401 that survives a recompose."""
        try:
            from ccswitch import ledger
            with ledger.locked_ledger() as led:
                ledger.set_status(led, account, "needs-reauth")
        except Exception:
            log.warning("recover.mark_needs_reauth failed account=%s", account, exc_info=True)

    async def switch_account_manual(self, session_id: str, new_account: str) -> None:
        """User-initiated account switch. Pins the session against usage-ceiling
        rotation, then resumes the conversation on new_account via the shared
        _reassign primitive (same path as auto-rotation / recovery)."""
        record = self._records.get(session_id)
        if record is None or session_id not in self._drivers:
            raise KeyError(session_id)
        # Validate: reject path-traversal / bad chars.
        assert_valid_account_name(new_account)
        # Validate: reject unknown accounts (must be in the configured ledger).
        known = {a["name"] for a in ccswitch_accounts.list_accounts()}
        if new_account not in known:
            raise KeyError(f"Unknown account: {new_account}")
        record.account_pinned = True
        await self._persist_pin(session_id, True)
        await self._reassign(record, new_account, reason="manual")

    async def _persist_pin(self, session_id: str, pinned: bool) -> None:
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id)
                .values(account_pinned=pinned)
            )

    async def set_model_for_session(self, session_id: str, model: str | None) -> None:
        """Set the live model AND persist it, so it survives rehydrate and every
        driver rebuild (rotation/recovery re-applies record.model in _reassign)."""
        driver = self._drivers.get(session_id)
        if driver is not None:
            await driver.set_model(model)
        record = self._records.get(session_id)
        if record is not None:
            record.model = model
        await self._persist_model(session_id, model)

    async def _persist_model(self, session_id: str, model: str | None) -> None:
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession).where(DbSession.id == session_id).values(model=model)
            )

    async def _reassign(self, record: SessionRecord, new_account: str, *, reason: str) -> None:
        """Resume the session's claude conversation on *new_account* (may equal the
        current account, for a 401 recompose). Relaunches the driver, replays the last
        prompt, updates the DB, and emits a rotated event."""
        old_driver = self._drivers.get(record.id)
        if old_driver is None:
            return
        old = record.account
        claude_sid = old_driver.claude_session_id
        try:
            await old_driver.interrupt()
            await old_driver.aclose()
            cfg = compose(new_account)
            configdir.trust_project(new_account, old_driver.cwd)
            new_driver = SessionDriver(
                record.id, new_account, old_driver.cwd, str(cfg),
                mode=old_driver.mode, sdk_factory=self._session_factory(record.id),
                registry=self._registry, identity=old_driver.identity,
                resume=claude_sid,
                on_session_id=self._persist_claude_session_id,
                on_sync_back=functools.partial(ccswitch_accounts.sync_back, new_account),
                on_status=self._persist_status,
                on_recoverable_failure=self._handle_recoverable_failure,
                suggester=self._suggester,
            )
            await new_driver.start()
            self._drivers[record.id] = new_driver
            if record.model is not None:
                await new_driver.set_model(record.model)
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
            log.info("reassign.complete session=%s %s→%s reason=%s",
                     record.id, old, new_account, reason)
            self._publish_event({
                "type": "rotated", "sessionId": record.id, "fromAccount": old,
                "toAccount": new_account, "reason": reason,
                "turnRestarted": bool(last),
            })
        except Exception:
            log.exception("reassign.error session=%s", record.id)

    # ------------------------------------------------------------------
    # Archive / unarchive / permanent-delete
    # ------------------------------------------------------------------

    async def archive_session(self, session_id: str) -> None:
        """Archive a session: stop driver, move cwd into archive_dir, status='archived'.

        The DB row is KEPT (status='archived'). The working directory is moved to
        archive_dir()/<session_id>/ so it can be reviewed or unarchived later.
        If the cwd is missing (already gone), skip the move but still mark archived.
        Git worktree registrations are pruned best-effort after the move.
        """
        from ccswitch.paths import archive_dir

        # Teardown the virtual X11 display — mirrors close_session.
        if self._x11 is not None:
            try:
                await self._x11.stop_display(session_id)
            except Exception:
                log.exception("session.display_cleanup_failed id=%s", session_id)

        # Stop the driver and clear in-memory state (mirrors close_session head).
        driver = self._drivers.pop(session_id, None)
        self._last_prompt.pop(session_id, None)
        self._pending_initial_prompt.pop(session_id, None)
        if driver is not None:
            await driver.aclose()

        # Remove from active records — archived sessions are NOT in _records.
        record = self._records.pop(session_id, None)

        # Clear in-memory worktree registration (we move the dir, not remove via git).
        self._worktrees.pop(session_id, None)

        # Move the cwd into the archive directory.
        dest = archive_dir() / session_id
        cwd_path: Path | None = Path(record.cwd) if (record and record.cwd) else None

        if cwd_path and cwd_path.exists():
            try:
                await asyncio.to_thread(shutil.move, str(cwd_path), str(dest))
                log.info(
                    "session.archive id=%s moved cwd=%s to archive=%s",
                    session_id, cwd_path, dest,
                )
            except Exception:
                log.exception(
                    "session.archive_move_failed id=%s src=%s dest=%s",
                    session_id, cwd_path, dest,
                )
        else:
            log.info("session.archive id=%s cwd missing or unknown, skip move", session_id)

        # Best-effort: prune stale git worktree registrations.
        if cwd_path:
            def _prune_worktree() -> None:
                try:
                    r = subprocess.run(
                        ["git", "-C", str(cwd_path.parent), "worktree", "prune"],
                        capture_output=True, timeout=10,
                    )
                    if r.returncode != 0:
                        log.debug(
                            "session.archive_worktree_prune_failed id=%s stderr=%s",
                            session_id, r.stderr,
                        )
                except Exception:
                    pass
            await asyncio.to_thread(_prune_worktree)

        # Mark archived in DB.
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession)
                .where(DbSession.id == session_id)
                .values(status="archived")
            )
        log.info("session.archive id=%s done", session_id)

    async def list_archived(self) -> list[dict]:
        """Return metadata dicts for all archived sessions (reads DB, not _records)."""
        from sqlalchemy import select as sa_select
        async with db_session() as db:
            rows = (await db.execute(
                sa_select(DbSession).where(DbSession.status == "archived")
            )).scalars().all()
        return [
            {
                "id": row.id,
                "name": row.name,
                "account": row.account_name,
                "cwd": row.cwd,
                "claudeSessionId": row.claude_session_id,
                "permissionMode": row.permission_mode,
                "createdAt": row.created_at.isoformat() if row.created_at else None,
                "archivedAt": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ]

    async def unarchive(self, session_id: str) -> "SessionRecord | None":
        """Move the archived cwd back and restore the session to idle.

        Returns the restored SessionRecord, or None if not found / not archived.
        Adds the record to _records so it reappears in list_sessions() without restart.
        """
        from ccswitch.paths import archive_dir
        from sqlalchemy import select as sa_select

        async with db_session() as db:
            row = (await db.execute(
                sa_select(DbSession)
                .where(DbSession.id == session_id)
                .where(DbSession.status == "archived")
            )).scalars().first()
        if row is None:
            return None

        archive_path = archive_dir() / session_id
        original_cwd = Path(row.cwd)

        def _restore() -> None:
            if archive_path.exists():
                original_cwd.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(archive_path), str(original_cwd))
            else:
                # Archive folder missing — recreate an empty cwd so SDK can start.
                original_cwd.mkdir(parents=True, exist_ok=True)
                log.warning(
                    "session.unarchive id=%s archive folder missing, recreating empty cwd",
                    session_id,
                )

        await asyncio.to_thread(_restore)

        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(DbSession)
                .where(DbSession.id == session_id)
                .values(status="idle")
            )

        record = SessionRecord(
            id=row.id,
            name=row.name,
            account=row.account_name,
            claude_session_id=row.claude_session_id or "",
            cols=80,
            rows=24,
            cwd=row.cwd,
            detached=True,
            status="idle",
            permission_mode=row.permission_mode,
            created_at=row.created_at,
            model=getattr(row, "model", None),
            account_pinned=bool(getattr(row, "account_pinned", False)),
        )
        self._records[session_id] = record
        log.info("session.unarchive id=%s restored to %s", session_id, original_cwd)
        return record

    async def permanent_delete(self, session_id: str) -> bool:
        """Permanently delete an archived session: remove archive folder + DB row.

        Returns True if deleted, False if the session was not found in DB.
        Containment guard: only removes paths that are direct children of archive_dir().
        """
        from ccswitch.paths import archive_dir
        from sqlalchemy import select as sa_select

        async with db_session() as db:
            row = (await db.execute(
                sa_select(DbSession).where(DbSession.id == session_id)
            )).scalars().first()
        if row is None:
            return False

        # Containment guard: candidate must be a direct child of archive_dir().
        archive_base = archive_dir().resolve()
        candidate = (archive_base / session_id).resolve()
        try:
            candidate.relative_to(archive_base)
        except ValueError:
            log.error(
                "session.permanent_delete CONTAINMENT GUARD id=%s candidate=%s",
                session_id, candidate,
            )
            return False

        await asyncio.to_thread(shutil.rmtree, str(candidate), True)
        log.info("session.permanent_delete id=%s archive_path=%s", session_id, candidate)

        async with db_session() as db:
            from sqlalchemy import delete as sa_delete
            # Remove the session's attachment rows + (dedup-aware) on-disk files
            # before the session row itself.
            n_att = await _attachments.delete_for_session(db, session_id)
            await db.execute(
                sa_delete(DbSession).where(DbSession.id == session_id)
            )
        if n_att:
            log.info("session.permanent_delete id=%s removed %d attachment(s)",
                     session_id, n_att)
        log.info("session.permanent_delete id=%s db row removed", session_id)
        return True

    async def transcript_events(self, session_id: str) -> list:
        """Return replayed transcript events for a session (read-only review).

        Works for both active and archived sessions: transcripts live in the
        account's config dir (not the moved cwd), so always accessible.
        Returns [] if no transcript is found or parsing fails.
        """
        from server.sessions.replay import replay_events

        path = self.transcript_path(session_id)
        if path is None:
            # For archived sessions the record is not in _records; load from DB.
            from sqlalchemy import select as sa_select
            import glob as _glob
            async with db_session() as db:
                row = (await db.execute(
                    sa_select(DbSession).where(DbSession.id == session_id)
                )).scalars().first()
            if row and row.claude_session_id:
                cfg_dir = str(compose(row.account_name))
                matches = _glob.glob(
                    str(Path(cfg_dir) / "projects" / "*" / f"{row.claude_session_id}.jsonl")
                )
                path = matches[0] if matches else None
        if not path:
            return []
        try:
            return await replay_events(path)
        except Exception:
            log.warning("session.transcript_events id=%s replay failed", session_id, exc_info=True)
            return []
