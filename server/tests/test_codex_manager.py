"""Per-account Codex app-server supervision: login, ref-counting, restart, drain."""
from __future__ import annotations

import asyncio
import contextlib
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
    kw.setdefault("codex_home_base", Path("/tmp/viberpit-codex-test"))
    kw.setdefault("credential_supplier", _credentials)
    kw.setdefault("home_factory", lambda base, account: Path(base) / account)
    kw.setdefault("idle_grace_s", 60.0)
    kw.setdefault("backoff_base_s", 0.0)
    kw.setdefault("backoff_max_s", 0.0)
    kw.setdefault("exit_grace_s", 0.05)
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


def test_a_refused_login_is_not_reported_as_a_credential_leak(caplog):
    """#1942: an authority refusal used to log `runner_holds_credential`."""
    from server.codex.external_auth import ExternalAuthUnavailable

    async def go():
        manager, spawned = _manager()

        async def refuse(account: str):
            raise ExternalAuthUnavailable("the credential authority refused a")

        manager._credential_supplier = refuse
        with pytest.raises(mgr_mod.CodexStartError) as exc:
            await manager.ensure("a")
        assert "credential cache" not in str(exc.value)
        assert spawned[0].stopped

    with caplog.at_level(logging.ERROR):
        asyncio.run(go())
    logged = " ".join(r.getMessage() + str(getattr(r, "__dict__", "")) for r in caplog.records)
    assert "runner_holds_credential" not in logged
    assert "external_auth_unavailable" in logged


def test_cancelling_startup_discards_the_half_started_client_and_process():
    async def go():
        entered_supplier = asyncio.Event()

        async def blocked_credentials(account: str):
            entered_supplier.set()
            await asyncio.Event().wait()

        manager, spawned = _manager(credential_supplier=blocked_credentials)
        startup = asyncio.create_task(manager.ensure("a"))
        await asyncio.wait_for(entered_supplier.wait(), 1.0)
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await startup

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


def test_attach_without_a_live_runner_raises_a_typed_failure():
    manager, _ = _manager()
    with pytest.raises(mgr_mod.CodexRunnerMissingError):
        manager.attach("a", "session-1")


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


def test_re_arming_the_idle_timer_does_not_accumulate_tasks():
    """One timer per account: every ensure/detach re-arms, and N arms left N tasks."""

    async def go():
        manager, _ = _manager(idle_grace_s=60.0)
        await manager.ensure("a")
        for _ in range(20):
            manager.attach("a", "s1")
            manager.detach("s1")
            await manager.ensure("a")
        await asyncio.sleep(0)
        armed = [
            t for t in asyncio.all_tasks()
            if t.get_name().startswith("codex-idle-") and not t.done()
        ]
        assert len(armed) == 1
        await manager.stop_all()

    asyncio.run(go())


def test_attach_without_a_runner_is_logged(caplog):
    """A give-up between ensure() and driver registration must not be silent."""

    async def go():
        manager, _ = _manager()
        with pytest.raises(mgr_mod.CodexRunnerMissingError):
            manager.attach("a", "s1")

    with caplog.at_level(logging.WARNING):
        asyncio.run(go())
    assert any("attach_without_runner" in r.getMessage() for r in caplog.records)


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


def test_a_crash_and_its_restart_file_as_one_warning_not_two(caplog):
    """#1858: the exit already files the fault at `classify_runner_ending`'s
    level; the recovery that follows must not mint a second one. Before the
    fix, `codex.manager.runner_restarted` logged at WARNING unconditionally,
    so every crash+restart cycle filed two error-sink fingerprints for one
    incident — the #1485/#1991 shape."""
    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        with caplog.at_level(logging.INFO, logger="server.codex.manager"):
            spawned[0].crash()
            await manager.wait_settled("a", timeout=2.0)
        await manager.stop_all()

    asyncio.run(go())

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, (
        f"one incident filed as {len(warnings)}: {[r.getMessage() for r in warnings]}"
    )
    assert getattr(warnings[0], "event", None) == "codex.manager.runner_crashed"
    restarted = [r for r in caplog.records
                 if getattr(r, "event", None) == "codex.manager.runner_restarted"]
    assert restarted, "the restart itself must still be logged, just not as a fault"
    assert restarted[0].levelno == logging.INFO


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


def test_a_token_from_before_a_give_up_is_indeterminate_after_a_re_ensure():
    """The fence must outlive the runner: a crash storm then a fresh ensure()."""

    async def go():
        manager, spawned = _manager(max_restarts=1)
        await manager.ensure("a")
        token = manager.begin_turn("a", "t1")
        for _ in range(4):
            live = spawned[-1]
            if not live.started:
                break
            live.crash()
            await manager.wait_settled("a", timeout=2.0)
        assert manager.client_for("a") is None  # gave up
        await manager.ensure("a")  # a later session brings the account back
        assert manager.generation_for("a") != token.generation
        assert manager.finish_turn(token) is mgr_mod.TurnOutcome.INDETERMINATE
        await manager.stop_all()

    asyncio.run(go())


def test_a_token_from_before_an_idle_reap_is_indeterminate_after_a_re_ensure():
    async def go():
        manager, _ = _manager(idle_grace_s=0.02)
        await manager.ensure("a")
        token = manager.begin_turn("a", "t1")
        await asyncio.sleep(0.1)  # the idle reaper takes the runner away
        assert manager.client_for("a") is None
        await manager.ensure("a")
        assert manager.generation_for("a") == 2  # generations are never reused
        assert manager.finish_turn(token) is mgr_mod.TurnOutcome.INDETERMINATE
        await manager.stop_all()

    asyncio.run(go())


def test_ensure_never_hands_back_a_client_from_an_in_flight_restart():
    """A caller landing mid-restart must wait, not receive the aclose()d client."""

    async def go():
        reached_backoff = asyncio.Event()
        release = asyncio.Event()

        async def sleep(seconds: float) -> None:
            reached_backoff.set()
            await release.wait()

        manager, spawned = _manager(sleep=sleep)
        await manager.ensure("a")
        doomed = manager.client_for("a")
        spawned[0].crash()
        # The supervisor has now discarded the dead client and is inside its backoff.
        await asyncio.wait_for(reached_backoff.wait(), 2.0)

        pending = asyncio.ensure_future(manager.ensure("a"))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not pending.done()  # serialized behind the restart, not served a corpse

        release.set()
        await asyncio.wait_for(pending, 2.0)
        assert len(spawned) == 2
        assert manager.client_for("a") is not doomed
        await manager.stop_all()

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


def test_giving_up_reports_why_not_just_the_restart_count(caplog):
    """#1856: the auto-filed issue for ``runner_unrecoverable`` carried only
    ``account`` and ``restarts`` — never the crash detail the supervisor had
    already computed for the notice, so seven occurrences over five days never
    said why the account's app-server was unrecoverable."""

    async def go():
        manager, spawned = _manager(max_restarts=1, backoff_base_s=0.0, backoff_max_s=0.0)
        await manager.ensure("a")
        with caplog.at_level(logging.ERROR, logger="server.codex.manager"):
            for _ in range(2):
                spawned[-1].crash()
                await manager.wait_settled("a", timeout=2.0)

        found = [
            r for r in caplog.records
            if getattr(r, "event", None) == "codex.manager.runner_unrecoverable"
        ]
        assert len(found) == 1
        assert found[0].fields["detail"], "no reason means the issue diagnoses nothing"

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
# how an ending is reported (#1857)
# ---------------------------------------------------------------------------
#
# Recovery is identical for all three endings and stays that way here; what the
# journal (and the auto-error sink behind it) is told about them is not.

_ENDING_EVENTS = frozenset({
    "codex.manager.runner_exited",
    "codex.manager.runner_crashed",
    "codex.manager.runner_client_closed",
})


@contextlib.contextmanager
def _capture_endings(sink: list[str]):
    """Collect every ending event the manager logs for the duration of the block.

    A handler rather than `caplog`, because the teardown paths this guards run
    inside `asyncio.run(...)` and assert on the ABSENCE of a record — a fixture
    whose propagation depends on the caller's log configuration would make "no
    ending was logged" pass for the wrong reason.
    """
    class _Sink(logging.Handler):
        def emit(self, record):
            event = getattr(record, "event", None)
            if event in _ENDING_EVENTS:
                sink.append(event)

    logger = logging.getLogger("server.codex.manager")
    handler = _Sink(level=logging.DEBUG)
    previous, logger.disabled = logger.disabled, False
    logger.addHandler(handler)
    try:
        yield sink
    finally:
        logger.removeHandler(handler)
        logger.disabled = previous


def _ending_record(caplog):
    found = [r for r in caplog.records if getattr(r, "event", None) in _ENDING_EVENTS]
    assert len(found) == 1, [getattr(r, "event", None) for r in caplog.records]
    return found[0]


def test_a_clean_self_exit_is_reported_as_an_exit_and_still_restarts(caplog):
    """#1857 exactly: ``exit_code=0`` with nothing in flight, 42 times. The
    app-server left on its own and the replacement was serving before any caller
    could notice — so the record says what happened, carries the post-mortem the
    ERROR form computed and threw away, and does not claim a crash."""

    async def go():
        now = [1000.0]
        manager, spawned = _manager(clock=lambda: now[0])
        await manager.ensure("a")
        manager.attach("a", "s1")
        with caplog.at_level(logging.WARNING, logger="server.codex.manager"):
            now[0] += 42.0
            spawned[0].crash(rc=0)
            await manager.wait_settled("a", timeout=2.0)

        record = _ending_record(caplog)
        assert record.event == "codex.manager.runner_exited"
        assert record.levelno == logging.WARNING
        assert record.fields["reason"] == "process_exited_clean"
        assert record.fields["exit_code"] == 0
        assert record.fields["uptime_s"] == 42.0
        assert record.fields["pid"] == spawned[0].pid
        assert record.fields["detail"] == "thread 'main' panicked"
        assert len(spawned) == 2, "recovery is unchanged; only the report differs"
        await manager.stop_all()

    asyncio.run(go())


def test_an_abnormal_death_is_still_reported_as_a_crash(caplog):
    """Guard against widening this into 'an ending is never an error'."""

    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        with caplog.at_level(logging.WARNING, logger="server.codex.manager"):
            spawned[0].crash(rc=1)
            await manager.wait_settled("a", timeout=2.0)

        record = _ending_record(caplog)
        assert record.event == "codex.manager.runner_crashed"
        assert record.levelno == logging.ERROR
        assert record.fields["reason"] == "process_crashed"
        await manager.stop_all()

    asyncio.run(go())


def test_a_clean_exit_that_interrupted_a_turn_is_reported_as_an_error(caplog):
    """The cost, not the exit status, sets the level: this one lost work."""

    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        manager.begin_turn("a", "t1")
        with caplog.at_level(logging.WARNING, logger="server.codex.manager"):
            spawned[0].crash(rc=0)
            await manager.wait_settled("a", timeout=2.0)

        record = _ending_record(caplog)
        assert record.event == "codex.manager.runner_exited"
        assert record.levelno == logging.ERROR
        assert record.fields["interrupted_turns"] == 1
        await manager.stop_all()

    asyncio.run(go())


def test_a_latched_client_reports_the_latch_not_a_process_exit(caplog):
    """#1444's shape: the connection died while the process was still alive, and
    the reason it latched is the whole diagnosis."""

    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        with caplog.at_level(logging.WARNING, logger="server.codex.manager"):
            spawned[0].server.eof()  # the client latches; the process lives on
            await manager.wait_settled("a", timeout=2.0)

        record = _ending_record(caplog)
        assert record.event == "codex.manager.runner_client_closed"
        assert record.levelno == logging.ERROR
        assert record.fields["reason"] == "client_closed"
        assert record.fields["detail"], "a latch with no reason diagnoses nothing"
        assert spawned[0].stopped, "the still-live process must still be reaped"
        await manager.stop_all()

    asyncio.run(go())


def _exit_after_eof(child: FakeCodexProcess, rc: int, delay_s: float) -> None:
    """stdout closes now and the process is reaped `delay_s` later — a real exit."""
    child.server.eof()
    asyncio.get_running_loop().call_later(delay_s, child.crash, rc)


def test_eof_followed_by_a_clean_exit_is_reported_as_the_exit_not_a_latch(caplog):
    """#2313: stdout EOF beats the reaped status by milliseconds. That is a
    process exiting, not the #1444 latch, and must carry the real exit code."""

    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        with caplog.at_level(logging.WARNING, logger="server.codex.manager"):
            _exit_after_eof(spawned[0], rc=0, delay_s=0.01)
            await manager.wait_settled("a", timeout=2.0)

        record = _ending_record(caplog)
        assert record.event == "codex.manager.runner_exited"
        assert record.levelno == logging.WARNING
        assert record.fields["exit_code"] == 0
        assert record.fields["reason"] == "process_exited_clean"
        await manager.stop_all()

    asyncio.run(go())


def test_eof_followed_by_a_crash_reports_the_real_status(caplog):
    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        with caplog.at_level(logging.WARNING, logger="server.codex.manager"):
            _exit_after_eof(spawned[0], rc=139, delay_s=0.01)
            await manager.wait_settled("a", timeout=2.0)

        record = _ending_record(caplog)
        assert record.event == "codex.manager.runner_crashed"
        assert record.fields["exit_code"] == 139
        await manager.stop_all()

    asyncio.run(go())


def test_eof_with_the_process_alive_past_the_grace_is_still_a_latch(caplog):
    """The #1444 shape survives the grace: the process outlives it, so the ending
    is still `client_closed` and the live process is still reaped."""

    async def go():
        manager, spawned = _manager(exit_grace_s=0.02)
        await manager.ensure("a")
        with caplog.at_level(logging.WARNING, logger="server.codex.manager"):
            _exit_after_eof(spawned[0], rc=0, delay_s=30.0)  # far beyond the grace
            await manager.wait_settled("a", timeout=2.0)

        record = _ending_record(caplog)
        assert record.event == "codex.manager.runner_client_closed"
        assert record.fields["exit_code"] == mgr_mod.CLIENT_CLOSED_EXIT_CODE
        assert spawned[0].stopped
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


def test_a_deliberate_stop_with_a_turn_in_flight_reports_no_ending():
    """`test_stop_is_not_a_crash` covers an IDLE stop and asserts only on
    notices; this covers the case a reader of #1857 would suspect — a stop that
    lands while a turn is open — and asserts on the LOG RECORD, which is what
    reaches the error sink and files an issue.

    Two redundant protections keep a deliberate teardown out of `_handle_exit`,
    and neither is load-bearing alone (verified by mutation: removing either one
    leaves this green, removing BOTH turns it red with
    ``runner_exited``):

    * ``stop()`` cancels the supervisor before discarding the process, so
      ``_await_runner_death`` never returns normally;
    * ``_supervise`` re-checks ``runner.stopping`` and the registry identity the
      moment it does return.

    Pinned at the record level because "a runner we asked to leave, reported as
    an ending" is the #1857 misclassification one path over.
    """

    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        manager.attach("a", "s1")
        seen = _notices(manager, "a")
        manager.begin_turn("a", "t1")
        caught: list[str] = []
        with _capture_endings(caught):
            await manager.stop("a")
            await asyncio.sleep(0.05)

        assert caught == [], f"a deliberate stop reported an ending: {caught}"
        assert seen == [], "a stop must not notify threads of a crash"
        assert len(spawned) == 1, "a stop must not respawn"

    asyncio.run(go())


def test_an_idle_reap_with_a_turn_in_flight_reports_no_ending():
    """The idle reaper tears down through the same `stop()`, so it inherits both
    protections above — asserted here rather than assumed, because the reaper
    fires on a timer nobody is watching and is by far the commonest way a runner
    disappears on a quiet host (six reaps on dobby in the hour #1857 was
    diagnosed, none of them a crash, all of them `idle_reap` → `runner_stopped`).

    Also pins that the reap still HAPPENS: an absence assertion that passed
    because the teardown silently stopped running would be no guard at all."""

    async def go():
        manager, spawned = _manager(idle_grace_s=0.05)
        await manager.ensure("a")
        manager.attach("a", "s1")
        seen = _notices(manager, "a")
        manager.begin_turn("a", "t1")
        caught: list[str] = []
        with _capture_endings(caught):
            manager.detach("s1")  # the last detach arms the reaper
            await asyncio.sleep(0.4)

        assert caught == [], f"an idle reap reported an ending: {caught}"
        assert seen == []
        assert len(spawned) == 1
        assert manager.client_for("a") is None, "the reaper must still have reaped"

    asyncio.run(go())


def test_stop_re_raises_an_outer_cancellation():
    """Swallowing it would eat the daemon drain's stop signal mid-teardown."""

    async def go():
        manager, _ = _manager()
        await manager.ensure("a")
        runner = manager._runners["a"]
        assert runner.supervisor is not None
        runner.supervisor.cancel()

        async def stubborn() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)  # slow to die: we get cancelled meanwhile
                raise

        runner.supervisor = asyncio.create_task(stubborn())
        await asyncio.sleep(0)
        stopping = asyncio.ensure_future(manager.stop("a"))
        await asyncio.sleep(0.01)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping

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


# ---------------------------------------------------------------------------
# a latched client is a dead runner (#1444)
# ---------------------------------------------------------------------------


async def _until(probe, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not probe():
        if loop.time() >= deadline:
            raise TimeoutError("condition not met")
        await asyncio.sleep(0.001)


def _oversized_frame() -> bytes:
    # Past the fake reader's 1 MiB limit: the client's read fails and latches,
    # while the (fake) process stays alive — exactly the shape seen on rocky.
    return b"x" * (2 * 1024 * 1024) + b"\n"


def test_a_client_that_latches_closed_while_the_process_lives_is_restarted():
    async def go():
        manager, spawned = _manager()
        await manager.ensure("a")
        manager.attach("a", "s1")
        seen = _notices(manager, "a")
        first = spawned[0]
        first.server.send_raw(_oversized_frame())
        await _until(lambda: len(spawned) == 2)
        await manager.wait_settled("a", timeout=2.0)
        assert first.stopped, "the process behind a dead connection must not leak"
        assert spawned[1].server.frames_for("initialize")
        assert spawned[1].server.frames_for("account/login/start")
        kinds = [n.kind for n in seen]
        assert mgr_mod.RunnerNoticeKind.CRASHED in kinds
        assert mgr_mod.RunnerNoticeKind.RESTARTED in kinds
        live = manager.client_for("a")
        assert live is not None and not live.closed
        await manager.stop_all()

    asyncio.run(go())


def test_client_for_hides_a_latched_client_during_the_restart_window():
    async def go():
        resume = asyncio.Event()

        async def sleep(seconds: float) -> None:
            await resume.wait()

        manager, spawned = _manager(sleep=sleep)
        await manager.ensure("a")
        assert manager.client_for("a") is not None
        spawned[0].server.send_raw(_oversized_frame())
        # Nobody may be handed the dead connection while the replacement is pending.
        await _until(lambda: manager.client_for("a") is None)
        resume.set()
        await _until(lambda: len(spawned) == 2)
        await manager.wait_settled("a", timeout=2.0)
        assert manager.client_for("a") is not None
        await manager.stop_all()

    asyncio.run(go())
