import os
from unittest import mock
from server.app import create_app
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.remote import RemoteOrchestrator


def test_flag_set_selects_remote_facade():
    with mock.patch.dict(os.environ, {"CCSWITCH_SESSIOND_SOCKET": "/tmp/ccsd-test.sock"}):
        app = create_app()
    assert app.state.remote_mode is True
    assert app.state.orchestrator is None
    assert isinstance(app.state.sessions, RemoteOrchestrator)


def test_flag_unset_is_in_process():
    env = {k: v for k, v in os.environ.items() if k != "CCSWITCH_SESSIOND_SOCKET"}
    with mock.patch.dict(os.environ, env, clear=True):
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
