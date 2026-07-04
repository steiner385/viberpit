import asyncio
import os
import signal
from unittest import mock
from server.app import create_app
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.remote import RemoteOrchestrator
import pytest
from starlette.testclient import TestClient


def test_flag_set_selects_remote_facade():
    with mock.patch.dict(os.environ, {"CCSWITCH_SESSIOND_SOCKET": "/tmp/ccsd-test.sock"}):
        app = create_app()
    assert app.state.remote_mode is True
    assert app.state.orchestrator is None
    assert isinstance(app.state.sessions, RemoteOrchestrator)


def test_flag_unset_is_in_process():
    # No flag AND no daemon socket on the host → in-process. Mock socket-absent so the
    # test is deterministic regardless of whether a real daemon happens to run here.
    env = {k: v for k, v in os.environ.items()
           if k not in ("CCSWITCH_SESSIOND_SOCKET", "CCSWITCH_SESSIOND_ENDPOINT")}
    with mock.patch.dict(os.environ, env, clear=True), \
         mock.patch("server.app._daemon_present", return_value=False):
        app = create_app()
    assert app.state.remote_mode is False
    assert app.state.orchestrator is not None
    assert isinstance(app.state.sessions, InProcessOrchestratorAdapter)


def test_injected_orchestrator_wins_over_flag():
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator()
    with mock.patch.dict(os.environ, {"CCSWITCH_SESSIOND_SOCKET": "/tmp/ccsd-test.sock"}):
        app = create_app(orchestrator=orch)
    assert app.state.remote_mode is False
    assert app.state.orchestrator is orch
    assert isinstance(app.state.sessions, InProcessOrchestratorAdapter)


@pytest.mark.asyncio
async def test_remote_lifespan_skips_orchestrator_and_closes_facade(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'edge.db'}"
    env = {
        "CCSWITCH_SESSIOND_SOCKET": str(tmp_path / "dead.sock"),  # no daemon listening
        "CCSWITCH_DB_URL": db_url,
        "CCSWITCH_DEV": "1",  # allow empty allowlist on the default bind
    }
    with mock.patch.dict(os.environ, env):
        app = create_app()
        closed = {}
        real_aclose = app.state.sessions.aclose

        async def _spy_aclose():
            closed["called"] = True
            await real_aclose()

        app.state.sessions.aclose = _spy_aclose
        # TestClient runs lifespan startup on enter, shutdown on exit.
        with TestClient(app):
            assert app.state.remote_mode is True
            assert app.state.orchestrator is None
        assert closed.get("called") is True


@pytest.mark.asyncio
async def test_lifespan_does_not_install_signal_handlers(tmp_path):
    """Regression (deploy SIGKILL): the edge lifespan must NOT register its own
    SIGTERM/SIGINT handlers. uvicorn installs handle_exit via loop.add_signal_handler
    before running lifespan startup; a handler registered here overwrites it, so
    SIGTERM never sets uvicorn's should_exit — the server hangs until systemd SIGKILLs
    it at TimeoutStopSec on every deploy restart, and this lifespan's own shutdown
    (session close + X11 sweep) never runs. Any running reauth is cancelled by the
    shutdown branch's reauth.shutdown(), so no signal handler is needed here."""
    env = {
        "CCSWITCH_SESSIOND_SOCKET": str(tmp_path / "dead.sock"),  # no daemon listening
        "CCSWITCH_DB_URL": f"sqlite+aiosqlite:///{tmp_path / 'edge.db'}",
        "CCSWITCH_DEV": "1",
    }
    loop = asyncio.get_running_loop()
    added: list[int] = []
    orig_add = loop.add_signal_handler

    def _spy(sig, cb, *args):
        added.append(sig)
        return orig_add(sig, cb, *args)

    with mock.patch.dict(os.environ, env), mock.patch.object(loop, "add_signal_handler", _spy):
        app = create_app()
        async with app.router.lifespan_context(app):
            pass

    assert signal.SIGTERM not in added, "lifespan clobbered uvicorn's SIGTERM handler"
    assert signal.SIGINT not in added, "lifespan clobbered uvicorn's SIGINT handler"
