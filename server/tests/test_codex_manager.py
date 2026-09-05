"""Per-account Codex app-server supervision: login, ref-counting, restart, drain."""
from __future__ import annotations

import asyncio
import itertools
import logging
import types
from pathlib import Path

import pytest

from server.codex import manager as mgr_mod
from server.codex import process as proc
from server.codex import schema
from server.codex.capabilities import CodexAvailability, CodexUnavailableReason
from server.codex.redaction import Redacted
from server.tests.fakes.fake_codex_app_server import FakeCodexAppServer

TOKEN = "eyJhbGciOiJIUzI1NiJ9.chatgpt-access-token.signature"
ACCOUNT_ID = "acct_1234567890"
LOCAL_MOUNTS = (("/", "ext4"), ("/tmp", "tmpfs"))

_pids = itertools.count(9000)


def _availability() -> CodexAvailability:
    return CodexAvailability.ready(
        binary_path="/usr/bin/codex", cli_version=schema.PINNED_CLI_VERSION
    )


async def _credentials(account: str) -> schema.ExternalAuthLoginParams:
    return schema.ExternalAuthLoginParams(
        access_token=Redacted(TOKEN),
        chatgpt_account_id=Redacted(ACCOUNT_ID),
        chatgpt_plan_type="pro",
    )


class FakeCodexProcess:
    """A `CodexProcessHandle` over an in-process fake app-server."""

    def __init__(self, account: str, home: Path, *, start_error: bool = False) -> None:
        self.account = account
        self.home = home
        self.pid = next(_pids)
        self.server = FakeCodexAppServer()
        self.server.rule("initialize", result=FakeCodexAppServer.default_initialize_response())
        self.server.rule("account/login/start", result={"type": "chatgptAuthTokens"})
        self.started = False
        self.stopped = False
        self._start_error = start_error
        self._exited = asyncio.Event()
        self._rc = 0

    @property
    def transport(self):
        return self.server.transport

    async def start(self) -> None:
        if self._start_error:
            raise proc.CodexProcessError("spawn refused")
        self.started = True
        self.server.start_autopilot()

    async def wait(self) -> int:
        await self._exited.wait()
        return self._rc

    async def stop(self, timeout: float = 5.0) -> None:
        self.stopped = True
        self._rc = 0
        self._exited.set()
        await self.server.stop_autopilot()
        self.server.eof()

    def stderr_tail(self) -> str:
        return "thread 'main' panicked"

    def crash(self, rc: int = 1) -> None:
        self._rc = rc
        self._exited.set()
        self.server.eof()


def _manager(**kw):
    spawned: list[FakeCodexProcess] = []
    start_errors = kw.pop("start_errors", 0)

    def factory(*, account: str, binary: str, home: Path):
        child = FakeCodexProcess(account, home, start_error=len(spawned) < start_errors)
        spawned.append(child)
        return child

    kw.setdefault("availability", _availability())
    kw.setdefault("codex_home_base", Path("/tmp/ccswitch-codex-test"))
    kw.setdefault("credential_supplier", _credentials)
    kw.setdefault("home_factory", lambda base, account: Path(base) / account)
    kw.setdefault("idle_grace_s", 60.0)
    kw.setdefault("backoff_base_s", 0.0)
    kw.setdefault("backoff_max_s", 0.0)
    manager = mgr_mod.CodexRunnerManager(process_factory=factory, **kw)
    return manager, spawned


# ---------------------------------------------------------------------------
# availability gate / fail-closed
# ---------------------------------------------------------------------------


def test_build_returns_none_when_codex_is_disabled():
    settings = types.SimpleNamespace(
        codex_enabled=False,
        codex_external_auth_enabled=False,
        codex_bin="codex",
        codex_supported_versions="",
        codex_idle_grace_s=300.0,
    )
    assert asyncio.run(mgr_mod.build_codex_manager(settings)) is None


def test_build_returns_none_when_the_binary_is_absent(monkeypatch):
    monkeypatch.setattr(proc, "resolve_codex_bin", lambda name: None)
    settings = types.SimpleNamespace(
        codex_enabled=True,
        codex_external_auth_enabled=True,
        codex_bin="codex",
        codex_supported_versions="",
        codex_idle_grace_s=300.0,
    )
    assert asyncio.run(mgr_mod.build_codex_manager(settings)) is None


def test_ensure_refuses_when_the_engine_is_unavailable():
    async def go():
        manager, spawned = _manager(
            availability=CodexAvailability.unavailable(
                CodexUnavailableReason.UNSUPPORTED_VERSION, "drifted"
            )
        )
        assert not manager.available
        with pytest.raises(mgr_mod.CodexUnavailableError):
            await manager.ensure("openai-main")
        assert spawned == []

    asyncio.run(go())


# ---------------------------------------------------------------------------
# ensure: serialized startup + login
# ---------------------------------------------------------------------------


def test_ensure_initializes_and_logs_in_with_the_supplied_credential():
    async def go():
        manager, spawned = _manager()
        await manager.ensure("openai-main")
        child = spawned[0]
        assert child.started
        assert child.server.frames_for("initialize")
        assert child.server.frames_for("initialized")
        login = child.server.frames_for("account/login/start")[0]
        assert login["params"]["type"] == "chatgptAuthTokens"
        assert login["params"]["accessToken"] == TOKEN
        assert manager.client_for("openai-main").initialized
        await manager.stop_all()

    asyncio.run(go())


def test_ensure_advertises_the_experimental_api_for_external_tokens():
    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        init = spawned[0].server.frames_for("initialize")[0]
        assert init["params"]["capabilities"]["experimentalApi"] is True
        await manager.stop_all()

    asyncio.run(go())


def test_ensure_is_idempotent():
    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        await manager.ensure("a")
        assert len(spawned) == 1
        await manager.stop_all()

    asyncio.run(go())


def test_concurrent_ensures_start_exactly_one_app_server():
    async def go():
        manager, spawned = _manager()
        await asyncio.gather(*(manager.ensure("a") for _ in range(5)))
        assert len(spawned) == 1
        await manager.stop_all()

    asyncio.run(go())


def test_one_app_server_per_account():
    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        await manager.ensure("b")
        assert {c.account for c in spawned} == {"a", "b"}
        await manager.stop_all()

    asyncio.run(go())


def test_a_failed_start_is_typed_and_leaves_no_runner():
    async def go():
        manager, spawned = _manager(start_errors=1)
        with pytest.raises(mgr_mod.CodexStartError):
            await manager.ensure("a")
        assert manager.client_for("a") is None
        await manager.ensure("a")  # the next attempt is allowed to try again
        assert len(spawned) == 2
        await manager.stop_all()

    asyncio.run(go())


def test_a_failed_login_tears_the_process_back_down():
    async def go():
        manager, spawned = _manager()

        async def refuse(account: str):
            raise RuntimeError("authority unreachable")

        manager._credential_supplier = refuse
        with pytest.raises(mgr_mod.CodexStartError):
            await manager.ensure("a")
        assert spawned[0].stopped
        assert manager.client_for("a") is None

    asyncio.run(go())


def test_no_credential_is_ever_logged(caplog):
    async def go():
        with caplog.at_level(logging.DEBUG):
            manager, _ = _manager()
            await manager.ensure("a")
            await manager.stop_all()

    asyncio.run(go())
    blob = "\n".join(r.getMessage() for r in caplog.records) + repr(caplog.records)
    assert TOKEN not in blob
    assert ACCOUNT_ID not in blob


def test_the_runner_home_holds_no_credential(tmp_path):
    """No credentials persisted on runners — the manager writes nothing but a dir."""

    async def go():
        manager, _ = _manager(codex_home_base=tmp_path, home_factory=None, mounts=LOCAL_MOUNTS)
        await manager.ensure("a")
        await manager.stop_all()

    asyncio.run(go())
    home = tmp_path / "a"
    assert home.is_dir()
    assert list(home.iterdir()) == []


# ---------------------------------------------------------------------------
# ref-counting and idle teardown
# ---------------------------------------------------------------------------


def test_detach_of_the_last_session_reaps_after_the_grace():
    async def go():
        manager, spawned = _manager(idle_grace_s=0.02)
        await manager.ensure("a")
        manager.attach("a", "s1")
        manager.detach("s1")
        await asyncio.sleep(0.1)
        assert manager.client_for("a") is None
        assert spawned[0].stopped

    asyncio.run(go())


def test_a_second_session_keeps_the_runner_alive():
    async def go():
        manager, spawned = _manager(idle_grace_s=0.02)
        await manager.ensure("a")
        manager.attach("a", "s1")
        manager.attach("a", "s2")
        manager.detach("s1")
        await asyncio.sleep(0.1)
        assert manager.client_for("a") is not None
        await manager.stop_all()

    asyncio.run(go())


def test_ref_count_is_keyed_by_session_so_repeats_cannot_strand_a_runner():
    async def go():
        manager, _ = _manager(idle_grace_s=0.02)
        await manager.ensure("a")
        manager.attach("a", "s1")
        manager.attach("a", "s1")  # duplicate attach
        manager.detach("s1")
        manager.detach("s1")  # duplicate detach
        manager.detach("never-attached")
        await asyncio.sleep(0.1)
        assert manager.client_for("a") is None

    asyncio.run(go())


def test_an_unattached_runner_is_reaped_so_a_failed_spawn_cannot_leak_it():
    async def go():
        manager, spawned = _manager(idle_grace_s=0.02)
        await manager.ensure("a")  # caller dies before attach()
        await asyncio.sleep(0.1)
        assert manager.client_for("a") is None
        assert spawned[0].stopped

    asyncio.run(go())


def test_re_ensuring_inside_the_grace_window_cancels_the_pending_reap():
    """A caller that ensures late in the idle window must not get a doomed client."""

    async def go():
        manager, spawned = _manager(idle_grace_s=0.05)
        await manager.ensure("a")
        manager.attach("a", "s1")
        manager.detach("s1")  # arms the reaper
        await asyncio.sleep(0.02)
        await manager.ensure("a")  # …and a new session claims it just in time
        manager.attach("a", "s2")
        await asyncio.sleep(0.1)
        assert manager.client_for("a") is not None
        assert not spawned[0].stopped
        await manager.stop_all()

    asyncio.run(go())


def test_attach_after_ensure_survives_the_idle_window():
    async def go():
        manager, _ = _manager(idle_grace_s=0.02)
        await manager.ensure("a")
        manager.attach("a", "s1")
        await asyncio.sleep(0.1)
        assert manager.client_for("a") is not None
        await manager.stop_all()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# crash, restart, and the not-completed guarantee
# ---------------------------------------------------------------------------


def _notices(manager, account, thread_id="t1"):
    seen: list[mgr_mod.RunnerNotice] = []
    manager.register_thread(account, thread_id, seen.append)
    return seen


def test_a_crash_restarts_reinitializes_and_relogs_in():
    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        manager.attach("a", "s1")
        seen = _notices(manager, "a")
        spawned[0].crash()
        await manager.wait_settled("a", timeout=2.0)
        assert len(spawned) == 2
        assert spawned[1].server.frames_for("initialize")
        assert spawned[1].server.frames_for("account/login/start")
        kinds = [n.kind for n in seen]
        assert mgr_mod.RunnerNoticeKind.CRASHED in kinds
        assert mgr_mod.RunnerNoticeKind.RESTARTED in kinds
        restarted = next(n for n in seen if n.kind is mgr_mod.RunnerNoticeKind.RESTARTED)
        assert restarted.recover_via == mgr_mod.RECOVER_VIA_THREAD_READ
        assert restarted.generation == 2
        await manager.stop_all()

    asyncio.run(go())


def test_an_in_flight_turn_is_never_reported_completed_after_a_crash():
    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        seen = _notices(manager, "a")
        token = manager.begin_turn("a", "t1")
        spawned[0].crash()
        await manager.wait_settled("a", timeout=2.0)
        # The caller believes its turn finished; the manager knows better.
        assert manager.finish_turn(token) is mgr_mod.TurnOutcome.INDETERMINATE
        crashed = next(n for n in seen if n.kind is mgr_mod.RunnerNoticeKind.CRASHED)
        assert crashed.interrupted_turns == ("t1",)
        assert crashed.outcome is mgr_mod.TurnOutcome.INDETERMINATE
        await manager.stop_all()

    asyncio.run(go())


def test_a_turn_that_spanned_no_restart_completes():
    async def go():
        manager, _ = _manager()
        await manager.ensure("a")
        token = manager.begin_turn("a", "t1")
        assert manager.finish_turn(token) is mgr_mod.TurnOutcome.COMPLETED
        await manager.stop_all()

    asyncio.run(go())


def test_a_turn_started_on_a_dead_runner_is_indeterminate():
    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        token = manager.begin_turn("a", "t1")
        await manager.stop("a")
        assert manager.finish_turn(token) is mgr_mod.TurnOutcome.INDETERMINATE

    asyncio.run(go())


def test_restart_is_bounded_and_backs_off_with_jitter():
    async def go():
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)
            await asyncio.sleep(0)

        manager, spawned = _manager(
            max_restarts=2, backoff_base_s=1.0, backoff_max_s=8.0,
            jitter=lambda: 0.5, sleep=sleep,
        )
        await manager.ensure("a")
        seen = _notices(manager, "a")
        for _ in range(4):
            live = spawned[-1]
            if not live.started:
                break
            live.crash()
            await manager.wait_settled("a", timeout=2.0)
        # 1 initial spawn + max_restarts restarts, then it stops trying.
        assert len(spawned) == 3
        assert slept == [1.0, 2.0]  # base, doubled — both scaled by the jitter
        assert seen[-1].kind is mgr_mod.RunnerNoticeKind.UNRECOVERABLE
        assert manager.client_for("a") is None

    asyncio.run(go())


def test_backoff_is_capped():
    async def go():
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        manager, spawned = _manager(
            max_restarts=6, backoff_base_s=1.0, backoff_max_s=3.0,
            jitter=lambda: 1.0, sleep=sleep,
        )
        await manager.ensure("a")
        for _ in range(6):
            spawned[-1].crash()
            await manager.wait_settled("a", timeout=2.0)
        assert max(slept) <= 3.0
        assert slept[-1] == 3.0
        await manager.stop_all()

    asyncio.run(go())


def test_a_long_healthy_run_resets_the_restart_budget():
    async def go():
        now = [1000.0]
        manager, spawned = _manager(
            max_restarts=1, healthy_uptime_s=60.0, clock=lambda: now[0]
        )
        await manager.ensure("a")
        spawned[0].crash()
        await manager.wait_settled("a", timeout=2.0)
        assert len(spawned) == 2
        now[0] += 3600.0  # the replacement ran healthily for an hour
        spawned[1].crash()
        await manager.wait_settled("a", timeout=2.0)
        assert len(spawned) == 3  # budget reset, not exhausted
        await manager.stop_all()

    asyncio.run(go())


def test_a_notifier_that_raises_cannot_break_supervision():
    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")

        def boom(notice):
            raise RuntimeError("subscriber exploded")

        manager.register_thread("a", "t1", boom)
        good: list[mgr_mod.RunnerNotice] = []
        manager.register_thread("a", "t2", good.append)
        spawned[0].crash()
        await manager.wait_settled("a", timeout=2.0)
        assert len(spawned) == 2
        assert good  # the second subscriber still heard about it
        assert manager.notify_errors >= 1
        await manager.stop_all()

    asyncio.run(go())


def test_unregister_thread_stops_delivery():
    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        seen = _notices(manager, "a")
        manager.unregister_thread("a", "t1")
        spawned[0].crash()
        await manager.wait_settled("a", timeout=2.0)
        assert seen == []
        await manager.stop_all()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# drain
# ---------------------------------------------------------------------------


def test_stop_is_not_a_crash():
    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        seen = _notices(manager, "a")
        await manager.stop("a")
        await asyncio.sleep(0.05)
        assert seen == []
        assert len(spawned) == 1  # no restart
        assert manager.client_for("a") is None

    asyncio.run(go())


def test_stop_all_drains_every_runner_and_is_idempotent():
    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        await manager.ensure("b")
        await manager.stop_all()
        assert all(c.stopped for c in spawned)
        await manager.stop_all()
        assert manager.client_for("a") is None

    asyncio.run(go())


def test_stop_all_survives_one_runner_failing_to_stop():
    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        await manager.ensure("b")

        async def explode(timeout: float = 5.0):
            raise OSError("no such process")

        spawned[0].stop = explode  # type: ignore[method-assign]
        await manager.stop_all()
        assert spawned[1].stopped

    asyncio.run(go())


# ---------------------------------------------------------------------------
# orphan reaping (startup self-healing, mirrors CopilotProxyManager.sweep_orphans)
# ---------------------------------------------------------------------------


class _ProcInfo:
    def __init__(self, pid, cmdline, environ=None):
        self.pid = pid
        self.cmdline = cmdline
        self.environ = environ or {}


def test_sweep_orphans_reaps_strays_under_our_home_base_only():
    async def go():
        manager, spawned = _manager(codex_home_base=Path("/var/lib/ccswitch-codex"))
        await manager.ensure("a")
        live_pid = spawned[0].pid
        killed: list[int] = []
        procs = [
            _ProcInfo(1, ["/usr/bin/codex", "app-server"],
                      {"CODEX_HOME": "/var/lib/ccswitch-codex/stale"}),
            _ProcInfo(live_pid, ["/usr/bin/codex", "app-server"],
                      {"CODEX_HOME": "/var/lib/ccswitch-codex/a"}),
            _ProcInfo(3, ["/usr/bin/codex", "exec", "hi"],
                      {"CODEX_HOME": "/var/lib/ccswitch-codex/other"}),
            _ProcInfo(4, ["/usr/bin/codex", "app-server"],
                      {"CODEX_HOME": "/home/someone/.codex"}),
            _ProcInfo(5, ["/usr/bin/claude", "--print"], {}),
        ]
        n = manager.sweep_orphans(
            lister=lambda: procs, killer=lambda pid: (killed.append(pid), True)[1]
        )
        assert killed == [1]
        assert n == 1
        await manager.stop_all()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# a Codex crash must never stop or degrade a Claude driver
# ---------------------------------------------------------------------------


class _FakeClaudeDriver:
    """Stand-in for a live Claude driver: it only has to keep making progress."""

    def __init__(self) -> None:
        self.turns = 0
        self.closed = False

    async def run_turn(self) -> None:
        for _ in range(50):
            await asyncio.sleep(0)
            self.turns += 1

    async def aclose(self) -> None:
        self.closed = True


def test_a_codex_crash_storm_never_disturbs_a_concurrent_claude_turn():
    async def go():
        loop = asyncio.get_running_loop()
        unhandled: list[dict] = []
        loop.set_exception_handler(lambda _loop, ctx: unhandled.append(ctx))

        claude = _FakeClaudeDriver()
        turn = asyncio.ensure_future(claude.run_turn())

        manager, spawned = _manager(max_restarts=2)
        await manager.ensure("a")
        for _ in range(4):
            live = spawned[-1]
            if not live.started:
                break
            live.crash()
            await manager.wait_settled("a", timeout=2.0)

        await asyncio.wait_for(turn, 2.0)
        assert claude.turns == 50
        assert not claude.closed
        assert unhandled == []
        assert manager.client_for("a") is None  # only Codex was disabled

    asyncio.run(go())


def test_orchestrator_only_ensures_codex_for_a_codex_engine_session():
    from server.sessions.engine import AgentEngine
    from server.sessions.orchestrator import SessionOrchestrator

    calls: list[str] = []

    class _Stub:
        available = True

        async def ensure(self, account: str) -> None:
            calls.append(account)

        def attach(self, account: str, session_id: str) -> None:
            calls.append(f"attach:{session_id}")

        def detach(self, session_id: str) -> None:
            calls.append(f"detach:{session_id}")

    async def go():
        orch = SessionOrchestrator(codex=_Stub())
        await orch._ensure_codex_runner(AgentEngine.CLAUDE_CODE, "anthropic-main")
        assert calls == []
        await orch._ensure_codex_runner(AgentEngine.CODEX, "openai-main")
        assert calls == ["openai-main"]

    asyncio.run(go())


def test_orchestrator_without_a_codex_manager_is_unchanged():
    from server.sessions.engine import AgentEngine
    from server.sessions.orchestrator import SessionOrchestrator

    async def go():
        orch = SessionOrchestrator()
        assert orch._codex is None
        await orch._ensure_codex_runner(AgentEngine.CODEX, "openai-main")  # no-op
        orch._drivers["s1"] = object()
        assert await orch._teardown_driver_state("s1") is not None

    asyncio.run(go())


def test_teardown_detaches_codex_and_survives_a_failing_manager():
    from server.sessions.orchestrator import SessionOrchestrator

    seen: list[str] = []

    class _Ok:
        def detach(self, session_id: str) -> None:
            seen.append(session_id)

    class _Broken:
        def detach(self, session_id: str) -> None:
            raise RuntimeError("codex manager is wedged")

    async def go():
        orch = SessionOrchestrator(codex=_Ok())
        sentinel = object()
        orch._drivers["s1"] = sentinel
        assert await orch._teardown_driver_state("s1") is sentinel
        assert seen == ["s1"]

        # A wedged Codex manager must not stop a Claude session from being torn down.
        orch2 = SessionOrchestrator(codex=_Broken())
        sentinel2 = object()
        orch2._drivers["s2"] = sentinel2
        assert await orch2._teardown_driver_state("s2") is sentinel2
        assert "s2" not in orch2._drivers

    asyncio.run(go())
