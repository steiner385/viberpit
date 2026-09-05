"""One supervised `codex app-server` per (runner, OpenAI account).

This is the Codex sibling of `server/sessions/copilot_proxy.py`, and deliberately
the same shape: resolve → spawn → gate → ref-count ``attach``/``detach`` → idle
teardown → ``stop_all`` → ``sweep_orphans``, with a ``build_*_manager(settings)``
factory that returns ``None`` when the feature is off or the binary is absent.
Constitution VI: there is one supervised-subprocess idiom in this repository and
this module reuses it rather than inventing a second.

What Codex adds on top of that idiom, and why:

* **Multiplexing.** One app-server serves every thread of one account on this
  runner, so startup and login are serialized under a per-account lock and the
  ref-count is over *sessions*, not processes. The ref-count is keyed by session
  id rather than an integer counter: a duplicated attach or a dropped detach then
  self-corrects instead of stranding a live process forever.
* **Restart is a supervised state machine, not a retry.** A crashed app-server is
  replaced with jitter and exponential backoff, bounded by ``max_restarts`` so an
  unbootable binary cannot spin. A replacement is re-initialized and re-logged-in
  before it is announced, and a run that stayed healthy for ``healthy_uptime_s``
  refills the budget so a month-old runner is not condemned by ancient failures.
* **A crash never manufactures a success.** Every turn is fenced by the runner
  *generation* it started on (:meth:`begin_turn` / :meth:`finish_turn`). If the
  process died in between — even if the caller believes the turn finished — the
  outcome is :data:`TurnOutcome.INDETERMINATE`. That is enforced by the manager,
  not by the good behaviour of callers, because "the model finished" and "the
  process that was running the model is gone" are indistinguishable from outside.
  Generations are monotonic **per account** and outlive the runner they fence, so
  a give-up (or an idle reap) followed by a fresh ``ensure`` cannot re-mint the
  number an in-flight token was fenced to.
  Threads are told through :class:`RunnerNotice` to recover via ``thread/read``
  (Phase 4 performs the call; this module defines and guarantees the contract).
* **Failure is contained.** Nothing in the supervision path may raise into a
  caller that is running a Claude session: subscriber callbacks are isolated and
  counted, and every teardown is best-effort.

Credentials. The manager never persists, logs, or environment-passes a token: it
asks an injected async supplier for a
:class:`~server.codex.schema.ExternalAuthLoginParams` (whose fields are
``Redacted``) and hands it straight to ``account/login/start`` over JSON-RPC
stdin. Phase 3 supplies the real authority-backed callable; nothing else changes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Any, Final

from server.aio import cancel_and_reap
from server.codex import process as codex_process
from server.codex import redaction, schema
from server.codex.capabilities import CodexAvailability, CodexUnavailableReason
from server.codex.process import CodexProcess, CodexProcessHandle
from server.codex.protocol import CodexAppServerClient, RefreshHandler
from server.obs.context import log_event
from server.sessions.adopt import _proc_lister, terminate_process_tree

logger = logging.getLogger(__name__)

#: How a thread driver is told to resynchronize after its app-server was replaced.
#: Phase 4 issues the call; the notice contract is fixed here so both halves of the
#: recovery cannot drift.
RECOVER_VIA_THREAD_READ: Final = "thread/read"

DEFAULT_IDLE_GRACE_S: Final = 300.0
DEFAULT_MAX_RESTARTS: Final = 3
DEFAULT_BACKOFF_BASE_S: Final = 0.5
DEFAULT_BACKOFF_MAX_S: Final = 30.0
DEFAULT_HEALTHY_UPTIME_S: Final = 60.0
DEFAULT_STOP_TIMEOUT_S: Final = 5.0
#: Bound on the login handshake so a wedged authority cannot hold the account lock.
DEFAULT_LOGIN_TIMEOUT_S: Final = 30.0


class TurnOutcome(StrEnum):
    """How a turn ended, from the supervisor's point of view."""

    COMPLETED = "completed"
    #: The app-server died while the turn was in flight. Whether the model did any
    #: work is unknowable from here; it is never reported as success.
    INDETERMINATE = "indeterminate"


class RunnerNoticeKind(StrEnum):
    CRASHED = "crashed"
    RESTARTED = "restarted"
    UNRECOVERABLE = "unrecoverable"


@dataclass(frozen=True, slots=True)
class RunnerNotice:
    """A supervision event delivered to every thread driver on one runner."""

    kind: RunnerNoticeKind
    account: str
    generation: int
    interrupted_turns: tuple[str, ...] = ()
    outcome: TurnOutcome | None = None
    recover_via: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class TurnToken:
    """The fence a caller must present to claim a turn completed."""

    account: str
    thread_id: str
    generation: int


class CodexManagerError(RuntimeError):
    """Base class for manager-level failures."""


class CodexUnavailableError(CodexManagerError):
    """Codex is not usable on this runner; the availability report says why."""

    def __init__(self, availability: CodexAvailability) -> None:
        self.availability = availability
        reason = availability.reason.value if availability.reason else "unknown"
        super().__init__(f"codex is unavailable ({reason}): {availability.detail}")


class CodexStartError(CodexManagerError):
    """An app-server could not be started, initialized, or logged in."""


class CodexRunnerMissingError(CodexManagerError):
    """A session tried to attach after its ensured runner disappeared."""


CredentialSupplier = Callable[[str], Awaitable[schema.ExternalAuthLoginParams]]
ThreadNotifier = Callable[[RunnerNotice], None]
ProcessFactory = Callable[..., CodexProcessHandle]
ClientFactory = Callable[..., CodexAppServerClient]
HomeFactory = Callable[[Path, str], Path]


def default_codex_home_base() -> Path:
    """Runner-LOCAL state root for Codex homes.

    Deliberately not ``ccswitch.paths.root()``: that is the account store, which on
    the deployed fleet is an sshfs mount shared between hosts. Codex keeps SQLite,
    WAL, history and thread state under ``CODEX_HOME``, and the plan forbids that on
    a network filesystem. ``ensure_codex_home`` still verifies the real mount, so a
    host whose ``$HOME`` is itself networked is refused rather than corrupted.
    """
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "ccswitch" / "codex"


@dataclass
class _Runner:
    """Live supervision state for one account's app-server."""

    account: str
    home: Path
    process: CodexProcessHandle
    client: CodexAppServerClient
    #: Never defaulted: the value is minted by ``CodexRunnerManager._next_generation``
    #: so that it survives the runner it fences. A per-runner counter would restart
    #: at 1 whenever the runner object is replaced, and a turn token minted before a
    #: crash would then compare equal to a fresh runner — reporting a lost turn as
    #: COMPLETED, which is the one thing this class exists to prevent.
    generation: int
    started_at: float = 0.0
    restarts: int = 0
    sessions: set[str] = field(default_factory=set)
    #: Bumped whenever the runner is claimed again. An armed idle teardown captures
    #: the epoch it was armed at and stands down if it changed, so a runner that was
    #: re-ensured inside the grace window cannot be reaped by the old timer.
    idle_epoch: int = 0
    stopping: bool = False
    settled: bool = True
    unrecoverable: bool = False
    supervisor: asyncio.Task[None] | None = None


class CodexRunnerManager:
    """Supervises one app-server per account and multiplexes thread drivers on it."""

    def __init__(
        self,
        *,
        availability: CodexAvailability,
        codex_home_base: str | Path,
        credential_supplier: CredentialSupplier,
        external_auth_enabled: bool = True,
        process_factory: ProcessFactory | None = None,
        client_factory: ClientFactory | None = None,
        home_factory: HomeFactory | None = None,
        refresh_handler: RefreshHandler | None = None,
        mounts: codex_process.Mounts | None = None,
        idle_grace_s: float = DEFAULT_IDLE_GRACE_S,
        max_restarts: int = DEFAULT_MAX_RESTARTS,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
        healthy_uptime_s: float = DEFAULT_HEALTHY_UPTIME_S,
        stop_timeout_s: float = DEFAULT_STOP_TIMEOUT_S,
        login_timeout_s: float = DEFAULT_LOGIN_TIMEOUT_S,
        jitter: Callable[[], float] = random.random,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._availability = availability
        self._home_base = Path(codex_home_base)
        self._credential_supplier = credential_supplier
        self._external_auth_enabled = external_auth_enabled
        self._process_factory = process_factory or self._default_process_factory
        self._client_factory = client_factory or self._default_client_factory
        self._home_factory = home_factory
        self._refresh_handler = refresh_handler
        self._mounts = mounts
        self._idle_grace_s = idle_grace_s
        self._max_restarts = max(0, max_restarts)
        self._backoff_base_s = max(0.0, backoff_base_s)
        self._backoff_max_s = max(0.0, backoff_max_s)
        self._healthy_uptime_s = healthy_uptime_s
        self._stop_timeout_s = stop_timeout_s
        self._login_timeout_s = login_timeout_s
        self._jitter = jitter
        self._sleep = sleep
        self._clock = clock

        self._runners: dict[str, _Runner] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()
        self._sessions: dict[str, str] = {}  # session id -> account
        self._notifiers: dict[str, dict[str, ThreadNotifier]] = {}
        self._turns: dict[str, dict[str, int]] = {}  # account -> thread -> generation
        #: Monotonic per account for the life of the manager. Owned here rather than
        #: on `_Runner` precisely so that stop(), the idle reaper and `_give_up` —
        #: all of which drop the runner — cannot rewind the fence.
        self._generations: dict[str, int] = {}
        #: At most one armed teardown per account; re-arming cancels the previous.
        self._idle_tasks: dict[str, asyncio.Task[None]] = {}
        self.notify_errors = 0

    def __repr__(self) -> str:
        return (
            f"<CodexRunnerManager available={self.available} "
            f"runners={sorted(self._runners)}>"
        )

    # -- introspection -----------------------------------------------------

    @property
    def available(self) -> bool:
        return self._availability.available

    @property
    def availability(self) -> CodexAvailability:
        return self._availability

    @property
    def accounts(self) -> tuple[str, ...]:
        return tuple(sorted(self._runners))

    def client_for(self, account: str) -> CodexAppServerClient | None:
        runner = self._runners.get(account)
        return runner.client if runner is not None else None

    def home_for(self, account: str) -> Path | None:
        runner = self._runners.get(account)
        return runner.home if runner is not None else None

    def generation_for(self, account: str) -> int | None:
        """The live runner's generation, or ``None`` when the account has no runner.

        Note that the *next* runner for this account will not reuse this number:
        generations are monotonic per account (see :meth:`_next_generation`).
        """
        runner = self._runners.get(account)
        return runner.generation if runner is not None else None

    def _next_generation(self, account: str) -> int:
        generation = self._generations.get(account, 0) + 1
        self._generations[account] = generation
        return generation

    # -- default factories -------------------------------------------------

    def _default_process_factory(
        self, *, account: str, binary: str, home: Path
    ) -> CodexProcessHandle:
        return CodexProcess(binary, home)

    def _default_client_factory(
        self, *, transport: Any, external_auth_enabled: bool
    ) -> CodexAppServerClient:
        return CodexAppServerClient(
            transport,
            external_auth_enabled=external_auth_enabled,
            refresh_handler=self._refresh_handler,
        )

    # -- startup -----------------------------------------------------------

    async def ensure(self, account: str) -> None:
        """Start (once) the app-server for *account*, initialized and logged in.

        Serialized per account: concurrent session spawns on one account produce
        exactly one process and exactly one login.
        """
        if not self.available:
            raise CodexUnavailableError(self._availability)
        lock = await self._lock_for(account)
        async with lock:
            existing = self._runners.get(account)
            if existing is not None:
                # Claiming an existing runner invalidates any teardown already armed
                # against it: without this, a caller that ensures at the very end of
                # the idle grace gets a client whose process is torn down underneath it.
                existing.idle_epoch += 1
                self._arm_idle_teardown(account)
                return
            runner = await self._start_runner(account)
            self._runners[account] = runner
            runner.supervisor = asyncio.create_task(
                self._supervise(runner), name=f"codex-supervisor-{account}"
            )
        # A runner nobody attaches to is idle from birth. Arming the timer here is
        # what keeps a spawn that fails *after* ensure() (a driver that never gets
        # built) from leaking a live app-server until daemon shutdown.
        self._arm_idle_teardown(account)

    async def _lock_for(self, account: str) -> asyncio.Lock:
        async with self._registry_lock:
            lock = self._locks.get(account)
            if lock is None:
                lock = self._locks[account] = asyncio.Lock()
            return lock

    async def _start_runner(self, account: str) -> _Runner:
        home = self._resolve_home(account)
        process = self._process_factory(
            account=account,
            binary=self._availability.binary_path or codex_process.CODEX_EXE,
            home=home,
        )
        client: CodexAppServerClient | None = None
        try:
            await process.start()
            client = self._client_factory(
                transport=process.transport,
                external_auth_enabled=self._external_auth_enabled,
            )
            await client.start()
            await client.initialize()
            await self._login(account, client)
        except asyncio.CancelledError:
            # Cancellation is an outcome, not an exemption from ownership cleanup:
            # process.start/client.start may already have allocated OS resources.
            await self._discard_startup(account, process, client)
            raise
        except Exception as exc:  # noqa: BLE001 -- one typed start failure
            # A daemon cancellation arriving while an ordinary startup failure is
            # being cleaned up has the same ownership rule: finish teardown first.
            await self._discard_startup(account, process, client)
            detail = redaction.scrub_exception(exc)
            tail = process.stderr_tail()
            log_event(
                logger,
                logging.ERROR,
                "codex.manager.start_failed",
                account=account,
                error=detail,
            )
            raise CodexStartError(
                f"codex app-server for {account} failed to start: {detail}"
                + (f" (stderr: {tail[-200:]})" if tail else "")
            ) from None
        log_event(
            logger,
            logging.INFO,
            "codex.manager.runner_started",
            account=account,
            pid=process.pid,
        )
        return _Runner(
            account=account,
            home=home,
            process=process,
            client=client,
            generation=self._next_generation(account),
            started_at=self._clock(),
        )

    def _resolve_home(self, account: str) -> Path:
        if self._home_factory is not None:
            return self._home_factory(self._home_base, account)
        return codex_process.ensure_codex_home(
            self._home_base, account, mounts=self._mounts
        )

    async def _login(self, account: str, client: CodexAppServerClient) -> None:
        """Hand the account's short-lived token to the app-server over stdin only."""
        params = await asyncio.wait_for(
            self._credential_supplier(account), self._login_timeout_s
        )
        await client.login_with_external_tokens(params)
        log_event(logger, logging.INFO, "codex.manager.logged_in", account=account)

    async def _discard(
        self, process: CodexProcessHandle | None, client: CodexAppServerClient | None
    ) -> None:
        """Best-effort teardown of a half-built or replaced runner."""
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 -- teardown must not raise
                logger.debug("codex: client close failed", exc_info=True)
        if process is not None:
            try:
                await process.stop(self._stop_timeout_s)
            except Exception:  # noqa: BLE001 -- teardown must not raise
                logger.debug("codex: process stop failed", exc_info=True)

    async def _discard_startup(
        self,
        account: str,
        process: CodexProcessHandle | None,
        client: CodexAppServerClient | None,
    ) -> None:
        """Finish half-built-runner cleanup even if the caller is cancelled."""
        cleanup = asyncio.create_task(
            self._discard(process, client), name=f"codex-start-discard-{account}"
        )
        cancelled_during_cleanup = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled_during_cleanup = True
        await cleanup
        if cancelled_during_cleanup:
            raise asyncio.CancelledError

    # -- ref-counting ------------------------------------------------------

    def attach(self, account: str, session_id: str) -> None:
        """Claim the account's runner for *session_id* (idempotent)."""
        runner = self._runners.get(account)
        if runner is None:
            # The runner was given up on (crash storm) or reaped between the caller's
            # ensure() and its driver registration. The session is now running with
            # nothing to claim; that must not be silent, or it looks like a healthy
            # attach right up until the first request fails.
            log_event(
                logger,
                logging.WARNING,
                "codex.manager.attach_without_runner",
                account=account,
                session=session_id,
            )
            raise CodexRunnerMissingError(
                f"codex runner for {account} disappeared before session {session_id} attached"
            )
        runner.sessions.add(session_id)
        self._sessions[session_id] = account

    def detach(self, session_id: str) -> None:
        """Release a session's claim; arm the idle reaper when it was the last one."""
        account = self._sessions.pop(session_id, None)
        if account is None:
            return
        runner = self._runners.get(account)
        if runner is None:
            return
        runner.sessions.discard(session_id)
        if not runner.sessions:
            self._arm_idle_teardown(account)

    def _arm_idle_teardown(self, account: str) -> None:
        """(Re-)arm the account's single idle timer.

        Every ``ensure()`` and every last-detach re-arms. Without cancelling the
        previous timer, N of those on one account leave N tasks sleeping out the
        grace window — harmless (the ``idle_epoch`` guard makes the stale ones
        no-ops) but unbounded, so exactly one is kept alive at a time.
        """
        runner = self._runners.get(account)
        if runner is None:
            return
        self._cancel_idle_task(account)
        task = asyncio.create_task(
            self._idle_teardown(account, runner.idle_epoch), name=f"codex-idle-{account}"
        )
        self._idle_tasks[account] = task
        task.add_done_callback(partial(self._forget_idle_task, account))

    def _cancel_idle_task(self, account: str) -> None:
        task = self._idle_tasks.pop(account, None)
        # A timer that is itself calling stop() must not cancel itself mid-teardown.
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _forget_idle_task(self, account: str, task: asyncio.Task[None]) -> None:
        if self._idle_tasks.get(account) is task:
            del self._idle_tasks[account]

    async def _idle_teardown(self, account: str, epoch: int) -> None:
        await asyncio.sleep(self._idle_grace_s)
        runner = self._runners.get(account)
        if runner is not None and not runner.sessions and runner.idle_epoch == epoch:
            log_event(logger, logging.INFO, "codex.manager.idle_reap", account=account)
            await self.stop(account)

    # -- turn fencing ------------------------------------------------------

    def begin_turn(self, account: str, thread_id: str) -> TurnToken:
        """Open a turn, fenced to the runner generation it starts on."""
        runner = self._runners.get(account)
        generation = runner.generation if runner is not None else -1
        self._turns.setdefault(account, {})[thread_id] = generation
        return TurnToken(account=account, thread_id=thread_id, generation=generation)

    def finish_turn(self, token: TurnToken) -> TurnOutcome:
        """Close a turn. ``COMPLETED`` only if the same process ran it throughout.

        The caller does not get to assert success: if the runner was replaced,
        removed, or given up on since :meth:`begin_turn`, the app-server that was
        executing the turn is gone and the result is unknowable, so it is
        INDETERMINATE. Because generations are monotonic *per account* and outlive
        the ``_Runner`` object, a crash-storm give-up (or an idle reap) followed by
        a fresh ``ensure()`` cannot re-mint the generation this token was fenced to.
        """
        self._turns.get(token.account, {}).pop(token.thread_id, None)
        runner = self._runners.get(token.account)
        current = self._generations.get(token.account, 0)
        if (
            runner is None
            or token.generation != runner.generation
            # A replacement whose generation is already minted but not yet installed:
            # the process that was running this turn has been discarded even though
            # the old runner object is still the registered one.
            or token.generation < current
        ):
            return TurnOutcome.INDETERMINATE
        return TurnOutcome.COMPLETED

    def active_turns(self, account: str) -> tuple[str, ...]:
        return tuple(sorted(self._turns.get(account, {})))

    # -- thread notification ----------------------------------------------

    def register_thread(
        self, account: str, thread_id: str, notifier: ThreadNotifier
    ) -> None:
        """Subscribe one thread driver to its runner's supervision events."""
        self._notifiers.setdefault(account, {})[thread_id] = notifier

    def unregister_thread(self, account: str, thread_id: str) -> None:
        self._notifiers.get(account, {}).pop(thread_id, None)
        self._turns.get(account, {}).pop(thread_id, None)

    def _notify(self, notice: RunnerNotice) -> None:
        for thread_id, notifier in list(self._notifiers.get(notice.account, {}).items()):
            try:
                notifier(notice)
            except Exception as exc:  # noqa: BLE001 -- one bad subscriber, not all
                self.notify_errors += 1
                log_event(
                    logger,
                    logging.ERROR,
                    "codex.manager.notify_failed",
                    account=notice.account,
                    thread=thread_id,
                    error=redaction.scrub_exception(exc),
                )

    # -- supervision -------------------------------------------------------

    async def _supervise(self, runner: _Runner) -> None:
        """Watch one account's app-server and replace it, boundedly, when it dies."""
        try:
            while True:
                try:
                    code = await runner.process.wait()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 -- treat an unwaitable child as dead
                    code = -1
                if runner.stopping or self._runners.get(runner.account) is not runner:
                    return
                runner.settled = False
                # The whole crash→discard→respawn→re-login sequence runs under the
                # SAME per-account lock that ensure() takes. Without it, ensure()
                # lands between the discard and the replacement, sees a runner
                # object that is still registered, and hands the caller a client
                # that has already been aclose()d. Startup and login stay serialized
                # per account (plan, Phase 2 Work bullet 3) in the restart path too.
                lock = await self._lock_for(runner.account)
                async with lock:
                    if runner.stopping or self._runners.get(runner.account) is not runner:
                        runner.settled = True
                        return
                    if not await self._handle_exit(runner, code):
                        return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- supervision must never escape as a task error
            logger.exception("codex: supervisor for %s failed", runner.account)
            runner.settled = True

    async def _handle_exit(self, runner: _Runner, code: int) -> bool:
        """React to one unexpected exit. Returns True when a replacement is live."""
        interrupted = self.active_turns(runner.account)
        detail = runner.process.stderr_tail()[-200:]
        log_event(
            logger,
            logging.ERROR,
            "codex.manager.runner_crashed",
            account=runner.account,
            generation=runner.generation,
            exit_code=code,
            interrupted_turns=len(interrupted),
        )
        # Announce the loss BEFORE attempting recovery: a thread driver must never
        # be able to observe a completed-looking turn across a crash window.
        self._notify(
            RunnerNotice(
                kind=RunnerNoticeKind.CRASHED,
                account=runner.account,
                generation=runner.generation,
                interrupted_turns=interrupted,
                outcome=TurnOutcome.INDETERMINATE,
                detail=detail,
            )
        )
        self._turns.pop(runner.account, None)
        await self._discard(runner.process, runner.client)

        if self._clock() - runner.started_at >= self._healthy_uptime_s:
            runner.restarts = 0  # a healthy run refills the budget

        while True:
            runner.restarts += 1
            if runner.restarts > self._max_restarts:
                await self._give_up(runner, detail)
                return False
            await self._sleep(self._backoff_delay(runner.restarts))
            if runner.stopping or self._runners.get(runner.account) is not runner:
                return False
            try:
                replacement = await self._start_runner(runner.account)
            except CodexStartError as exc:
                detail = redaction.scrub_exception(exc)
                continue
            runner.process = replacement.process
            runner.client = replacement.client
            runner.home = replacement.home
            runner.generation = replacement.generation  # minted monotonically per account
            runner.started_at = replacement.started_at
            runner.settled = True
            log_event(
                logger,
                logging.WARNING,
                "codex.manager.runner_restarted",
                account=runner.account,
                generation=runner.generation,
                attempt=runner.restarts,
            )
            self._notify(
                RunnerNotice(
                    kind=RunnerNoticeKind.RESTARTED,
                    account=runner.account,
                    generation=runner.generation,
                    recover_via=RECOVER_VIA_THREAD_READ,
                    detail=detail,
                )
            )
            return True

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter, clamped to ``backoff_max_s``."""
        raw = self._backoff_base_s * (2 ** max(0, attempt - 1))
        raw = min(raw, self._backoff_max_s) if self._backoff_max_s else raw
        jittered = raw * (0.5 + self._jitter())
        return min(jittered, self._backoff_max_s) if self._backoff_max_s else jittered

    async def _give_up(self, runner: _Runner, detail: str) -> None:
        """Stop trying. Codex for this account is off until something calls ensure."""
        runner.unrecoverable = True
        runner.settled = True
        self._runners.pop(runner.account, None)
        for session_id in list(runner.sessions):
            self._sessions.pop(session_id, None)
        runner.sessions.clear()
        log_event(
            logger,
            logging.ERROR,
            "codex.manager.runner_unrecoverable",
            account=runner.account,
            restarts=runner.restarts - 1,
        )
        self._notify(
            RunnerNotice(
                kind=RunnerNoticeKind.UNRECOVERABLE,
                account=runner.account,
                generation=runner.generation,
                outcome=TurnOutcome.INDETERMINATE,
                detail=detail,
            )
        )

    async def wait_settled(self, account: str, *, timeout: float = 5.0) -> None:
        """Wait until supervision is quiescent: ready again, or given up.

        The recovery barrier a thread driver waits on after a ``CRASHED`` notice
        before it re-reads its thread. Returns immediately when the account has no
        runner at all (there is nothing left to settle).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        # Yield first: an exit that has just fired may not have reached the
        # supervisor's continuation yet, and reading `settled` before it does would
        # report the pre-crash state.
        for _ in range(3):
            await asyncio.sleep(0)
        while True:
            runner = self._runners.get(account)
            if runner is None or runner.settled:
                return
            if loop.time() >= deadline:
                raise TimeoutError(f"codex runner for {account} did not settle")
            await asyncio.sleep(0.001)

    # -- teardown ----------------------------------------------------------

    async def stop(self, account: str) -> None:
        """Tear down one account's runner. A deliberate stop is never a crash."""
        runner = self._runners.pop(account, None)
        if runner is None:
            return
        runner.stopping = True
        for session_id in list(runner.sessions):
            self._sessions.pop(session_id, None)
        runner.sessions.clear()
        self._turns.pop(account, None)
        self._cancel_idle_task(account)
        supervisor, runner.supervisor = runner.supervisor, None
        if supervisor is not None:
            supervisor.cancel()
        await self._discard(runner.process, runner.client)
        # The supervisor answering the cancel() above is the expected path; swallowing
        # an OUTER cancellation here would eat the daemon drain's stop signal
        # mid-teardown, so reap it through the shared helper (issue #1164).
        await cancel_and_reap(supervisor, phase=f"codex.supervisor:{account}")
        log_event(logger, logging.INFO, "codex.manager.runner_stopped", account=account)

    async def stop_all(self) -> None:
        """Drain every runner concurrently — the daemon-shutdown sweep.

        ``return_exceptions`` so one wedged app-server cannot abort the drain of
        the rest, exactly as ``CopilotProxyManager.stop_all`` does.
        """
        accounts = list(self._runners)
        if not accounts:
            return
        await asyncio.gather(
            *(self.stop(account) for account in accounts), return_exceptions=True
        )

    # -- orphan reaping ----------------------------------------------------

    def sweep_orphans(
        self,
        *,
        lister: Callable[[], Iterable[Any]] = _proc_lister,
        killer: Callable[[int], bool] = lambda pid: terminate_process_tree(pid, grace=0.5),
    ) -> int:
        """Reap `codex app-server` processes from a prior run under OUR home base.

        Scoped twice over, because unlike ``copilot2api`` the ``codex`` binary is
        also something a human may be running: only the ``app-server`` subcommand
        counts, and only when its ``CODEX_HOME`` is inside this manager's base. A
        developer's own ``codex`` session, or another install's app-server, is left
        alone. At process start ``_runners`` is empty, so a fresh boot reaps every
        stray of ours — what is needed after a SIGKILLed shutdown.
        """
        live_pids = {
            r.process.pid for r in self._runners.values() if r.process.pid is not None
        }
        base = str(self._home_base)
        killed = 0
        for candidate in lister():
            cmd = list(getattr(candidate, "cmdline", None) or [])
            if len(cmd) < 2 or os.path.basename(cmd[0]) != codex_process.CODEX_EXE:
                continue
            if cmd[1] not in codex_process.APP_SERVER_ARGV:
                continue
            home = (getattr(candidate, "environ", None) or {}).get("CODEX_HOME", "")
            if not home or not (home == base or home.startswith(base.rstrip("/") + "/")):
                continue
            pid = getattr(candidate, "pid", None)
            if pid is None or pid in live_pids:
                continue
            try:
                if killer(pid):
                    killed += 1
            except Exception:  # noqa: BLE001 -- one failed reap must not stop the sweep
                logger.exception("codex: orphan reap failed pid=%s", pid)
        if killed:
            log_event(logger, logging.INFO, "codex.manager.orphans_reaped", count=killed)
        return killed


def _supported_versions(settings: Any) -> tuple[str, ...]:
    raw = getattr(settings, "codex_supported_versions", "") or ""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


async def build_codex_manager(settings: Any) -> CodexRunnerManager | None:
    """Factory: ``None`` (with a warning) when Codex is off or unusable.

    Mirrors ``build_copilot_proxy_manager``. Returning ``None`` — rather than a
    manager that raises — is what makes the feature flag fail-closed: with
    ``CCSWITCH_CODEX_ENABLED`` false, the orchestrator's Codex hooks are wired to
    nothing and no other engine's behaviour changes.

    The credential supplier is a placeholder until Phase 3 lands the authority
    client: it refuses, so a Codex session started before then fails closed with a
    typed error instead of inventing a credential.
    """
    availability = await codex_process.probe_availability(
        enabled=bool(getattr(settings, "codex_enabled", False)),
        external_auth_enabled=bool(getattr(settings, "codex_external_auth_enabled", False)),
        binary=getattr(settings, "codex_bin", codex_process.CODEX_EXE),
        supported_versions=_supported_versions(settings),
    )
    if not availability.available:
        level = (
            logging.DEBUG
            if availability.reason is CodexUnavailableReason.DISABLED
            else logging.WARNING
        )
        log_event(
            logger,
            level,
            "codex.manager.disabled",
            reason=availability.reason.value if availability.reason else "unknown",
            detail=availability.detail,
        )
        return None

    async def _no_authority(account: str) -> schema.ExternalAuthLoginParams:
        raise CodexStartError(
            "no ChatGPT credential authority is configured for "
            f"{account}: the Phase 3 authority client is not wired yet"
        )

    log_event(
        logger,
        logging.INFO,
        "codex.manager.enabled",
        cli_version=availability.cli_version,
        binary=availability.binary_path,
    )
    return CodexRunnerManager(
        availability=availability,
        codex_home_base=default_codex_home_base(),
        credential_supplier=_no_authority,
        external_auth_enabled=bool(
            getattr(settings, "codex_external_auth_enabled", False)
        ),
        idle_grace_s=float(getattr(settings, "codex_idle_grace_s", DEFAULT_IDLE_GRACE_S)),
    )
