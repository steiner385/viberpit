# server/tests/test_ipc_serve.py
import asyncio
from unittest import mock
import pytest
from server.ipc.serve import build_daemon, daemon_lifespan
from server.ipc.client import DaemonClient, protocol_compatible
from server.tests.ipc_harness import serve_daemon_uds
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord

_FAKE_X11 = object()  # sentinel — build_x11_manager returns None in X11-less CI


def test_build_daemon_wires_orchestrator_and_rpc():
    orch = mock.MagicMock()
    with mock.patch("server.ipc.serve.build_x11_manager", return_value=_FAKE_X11):
        app, returned = build_daemon(orchestrator=orch)
    assert returned is orch
    # x11 manager shared onto the orchestrator (mirrors the edge).
    assert orch._x11 is not None
    # RPC surface registered (a representative method).
    assert "ensure_driver" in app.state.rpc
    assert "list_sessions" in app.state.rpc


@pytest.mark.asyncio
async def test_daemon_lifespan_starts_and_stops_orchestrator():
    orch = mock.MagicMock()
    orch.rehydrate = mock.AsyncMock()
    orch.start = mock.AsyncMock()
    orch.stop = mock.AsyncMock()
    with mock.patch("server.ipc.serve.build_x11_manager", return_value=_FAKE_X11):
        app, _ = build_daemon(orchestrator=orch)
    with mock.patch("server.ipc.serve.init_db", new=mock.AsyncMock()) as initdb:
        async with daemon_lifespan(app, orch):
            initdb.assert_awaited_once()
            orch.start.assert_awaited_once()
        orch.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_orphan_clis_are_reaped_before_anything_resumes():
    """#1379: a `claude` that outlived the previous unit keeps editing files and can
    push. It MUST be gone before rehydrate/auto-resume can bring the same
    conversation back, or the session briefly has two live writers again."""
    calls: list[str] = []
    orch = mock.MagicMock()
    for name in ("reap_orphan_clis", "rehydrate", "rearm_background_tasks",
                 "auto_resume_interrupted", "start", "stop"):
        setattr(orch, name, mock.AsyncMock(side_effect=
                                           lambda _n=name, *a, **k: calls.append(_n)))
    with mock.patch("server.ipc.serve.build_x11_manager", return_value=_FAKE_X11):
        app, _ = build_daemon(orchestrator=orch)
    with mock.patch("server.ipc.serve.init_db", new=mock.AsyncMock()):
        async with daemon_lifespan(app, orch):
            await app.state.boot_resume_task
    assert calls.index("reap_orphan_clis") < calls.index("rehydrate")
    assert calls.index("reap_orphan_clis") < calls.index("auto_resume_interrupted")


@pytest.mark.asyncio
async def test_a_failing_orphan_reap_never_blocks_the_boot():
    """Same contract as the Codex and X11 orphan sweeps it sits beside: a
    diagnostic sweep may not stop a daemon that owns live sessions from starting."""
    orch = mock.MagicMock()
    orch.reap_orphan_clis = mock.AsyncMock(side_effect=OSError("/proc unreadable"))
    orch.rehydrate = mock.AsyncMock()
    orch.start = mock.AsyncMock()
    orch.stop = mock.AsyncMock()
    with mock.patch("server.ipc.serve.build_x11_manager", return_value=_FAKE_X11):
        app, _ = build_daemon(orchestrator=orch)
    with mock.patch("server.ipc.serve.init_db", new=mock.AsyncMock()):
        async with daemon_lifespan(app, orch):
            orch.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_built_daemon_serves_handshake_and_rpc_over_uds():
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    orch._records["s1"] = SessionRecord(id="s1", name="S1", account="acct",
                                        engine=AgentEngine.CLAUDE_CODE,
                                        engine_session_id="cs", cols=80, rows=24, cwd="/tmp")
    app, _ = build_daemon(orchestrator=orch)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            info = await client.info()
            assert protocol_compatible(info["protocol"]) is True
            listed = await client.call("list_sessions")
            assert [r["id"] for r in listed["sessions"]] == ["s1"]
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_daemon_lifespan_installs_drain_on_sigterm_and_uninstalls_on_exit():
    """The daemon must protect in-flight turns on SIGTERM itself (see
    test_daemon_drain_on_sigterm.py) — wired for the lifetime of the lifespan."""
    orch = mock.MagicMock()
    orch.rehydrate = mock.AsyncMock()
    orch.start = mock.AsyncMock()
    orch.stop = mock.AsyncMock()
    with mock.patch("server.ipc.serve.build_x11_manager", return_value=_FAKE_X11):
        app, _ = build_daemon(orchestrator=orch)
    uninstall = mock.MagicMock()
    with mock.patch("server.ipc.serve.init_db", new=mock.AsyncMock()), \
         mock.patch("server.ipc.serve.install_drain_on_sigterm", return_value=uninstall) as install:
        async with daemon_lifespan(app, orch):
            install.assert_called_once_with(orch)
            uninstall.assert_not_called()
        uninstall.assert_called_once()


def test_build_daemon_registers_the_app_token_provider():
    """The daemon is where ensure_clone runs, so it owns the git token provider.

    Without this wiring git fetch falls back to the machine's ambient credential
    helper — the failure mode that pauses the backlog with a stale-credential
    banner (issue #887)."""
    from viberpit import gitrepos
    gitrepos.set_token_provider(None)
    try:
        orch = mock.MagicMock()
        with mock.patch("server.ipc.serve.build_x11_manager", return_value=_FAKE_X11):
            build_daemon(orchestrator=orch)
        assert gitrepos._token_provider is not None
        # Delegates to the App-installation minting path, per owner.
        with mock.patch(
            "server.github.app_auth.token_for_owner", return_value="ghs_x"
        ) as mint:
            assert gitrepos._app_token("cairnea") == "ghs_x"
        mint.assert_called_once_with("cairnea")
    finally:
        gitrepos.set_token_provider(None)


def _lifespan_orch(calls: list[str], **overrides):
    orch = mock.MagicMock()
    for name in ("reap_orphan_clis", "rehydrate", "rearm_background_tasks",
                 "auto_resume_interrupted", "start", "stop"):
        setattr(orch, name, overrides.get(name) or mock.AsyncMock(
            side_effect=lambda _n=name, *a, **k: calls.append(_n)))
    return orch


@pytest.mark.asyncio
async def test_a_slow_rearm_never_holds_the_daemon_off_its_socket():
    """Uvicorn binds only once the lifespan yields. Re-arming drain-interrupted
    sessions resumes each one under the global `_ensure_lock` (~1 min apiece), so
    awaiting it inline kept rocky off :9443 for 6.5 min and failed the deploy's
    180 s verify ("daemon did not report <sha>"). The pass must run behind the yield."""
    release = asyncio.Event()
    calls: list[str] = []

    async def _blocked_rearm():
        calls.append("rearm_background_tasks")
        await release.wait()

    orch = _lifespan_orch(
        calls, rearm_background_tasks=mock.AsyncMock(side_effect=_blocked_rearm))
    with mock.patch("server.ipc.serve.build_x11_manager", return_value=_FAKE_X11):
        app, _ = build_daemon(orchestrator=orch)
    with mock.patch("server.ipc.serve.init_db", new=mock.AsyncMock()):
        cm = daemon_lifespan(app, orch)
        await asyncio.wait_for(cm.__aenter__(), timeout=2)  # yielded with rearm blocked
        try:
            orch.start.assert_awaited_once()
            await asyncio.sleep(0)
            assert "rearm_background_tasks" in calls
            assert "auto_resume_interrupted" not in calls  # still ordered after rearm
            task = app.state.boot_resume_task
            assert not task.done()
        finally:
            await cm.__aexit__(None, None, None)
        # Shutdown reaps the in-flight pass instead of waiting it out.
        assert task.cancelled()
        orch.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_boot_resume_pass_runs_rearm_then_auto_resume_after_drain_is_armed():
    """The SIGTERM drain hook must already be installed while sessions are being
    re-armed: a stop that lands mid-pass still drains the turns it just started."""
    calls: list[str] = []
    orch = _lifespan_orch(calls)
    with mock.patch("server.ipc.serve.build_x11_manager", return_value=_FAKE_X11):
        app, _ = build_daemon(orchestrator=orch)
    with mock.patch("server.ipc.serve.init_db", new=mock.AsyncMock()), \
         mock.patch("server.ipc.serve.install_drain_on_sigterm",
                    side_effect=lambda _o: calls.append("drain") or mock.MagicMock()):
        async with daemon_lifespan(app, orch):
            await app.state.boot_resume_task
    assert calls.index("rehydrate") < calls.index("drain") < calls.index(
        "rearm_background_tasks") < calls.index("auto_resume_interrupted")


class _Store:
    """awaiting_renewal() answers from a script, then stays at its last value."""
    def __init__(self, *answers):
        self.answers = list(answers)

    def awaiting_renewal(self):
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


@pytest.mark.asyncio
async def test_boot_pass_waits_for_the_edges_first_creds_push_before_rearming():
    """A worker's cached envelope lease (120 s) is always lapsed after a deploy
    restart (the drain alone runs ~3 min). Re-arming before the edge's next
    creds_sync launches every session with no token, so each one 401s and burns
    an auth-recovery relaunch — the "unauthenticated after deploy" symptom."""
    from server.ipc import serve as serve_mod
    calls: list[str] = []
    store = _Store(True, True, False)
    orch = _lifespan_orch(calls)
    with mock.patch.object(serve_mod.fleet_role, "is_worker", return_value=True), \
         mock.patch.object(serve_mod.credential_envelope, "runner_store", return_value=store):
        await serve_mod._boot_resume_pass(orch, creds_wait_s=5, creds_poll_s=0)
    assert store.answers == [False]  # polled until the lease came back
    assert calls == ["rearm_background_tasks", "auto_resume_interrupted"]


@pytest.mark.asyncio
async def test_boot_pass_wait_is_bounded_and_skipped_off_workers():
    from server.ipc import serve as serve_mod
    calls: list[str] = []
    orch = _lifespan_orch(calls)
    with mock.patch.object(serve_mod.fleet_role, "is_worker", return_value=True), \
         mock.patch.object(serve_mod.credential_envelope, "runner_store",
                           return_value=_Store(True)):
        await asyncio.wait_for(
            serve_mod._boot_resume_pass(orch, creds_wait_s=0.05, creds_poll_s=0.01), 2)
    assert calls == ["rearm_background_tasks", "auto_resume_interrupted"]

    calls.clear()
    with mock.patch.object(serve_mod.fleet_role, "is_worker", return_value=False), \
         mock.patch.object(serve_mod.credential_envelope, "runner_store") as rs:
        await serve_mod._boot_resume_pass(orch, creds_wait_s=60, creds_poll_s=0)
    rs.assert_not_called()  # the primary mints its own credentials
    assert calls == ["rearm_background_tasks", "auto_resume_interrupted"]
