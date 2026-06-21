"""Tests for orchestrator.answer_question — mirrors resolve_permission pattern."""
import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient

REPO_ROOT = Path(__file__).parent.parent.parent


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from ccswitch.oauth import Usage
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return Usage(10.0, reset, 10.0, reset, False)


def _seed_account(name: str) -> None:
    import time as _time
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok", "refreshToken": f"{name}-ref",
        "expiresAt": int(_time.time() * 1000) + 9_000_000, "subscriptionType": "max",
    })
    if not paths.shared_mcp_path().exists():
        write_json_secure(paths.shared_mcp_path(), {})


@pytest.fixture()
def orch_env(tmp_path):
    store_home = tmp_path / "store"
    store_home.mkdir()
    shared_base = tmp_path / "dot-claude"
    shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"
    cfg_dir.mkdir(parents=True)

    env_patch = patch.dict(os.environ, {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    })
    usage_patch = patch("ccswitch.accounts.usage_for", side_effect=_fake_usage)
    compose_patch = patch("server.sessions.orchestrator.compose", return_value=cfg_dir)
    select_patch = patch("server.sessions.orchestrator.select_account", return_value="alpha")

    with env_patch, usage_patch, compose_patch, select_patch:
        _seed_account("alpha")

        def _factory(msgs):
            def make(cwd, config_dir, can_use_tool, mode, resume):
                return FakeSDKClient(list(msgs), on_can_use_tool=can_use_tool)
            return make

        orch = SessionOrchestrator(
            sdk_factory=_factory([]),
            work_dir=str(REPO_ROOT),
            poll_interval=9999.0,
            usage_fn=_fake_usage,
        )
        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            yield orch, client


def test_answer_question_unknown_session_returns_false(orch_env):
    """answer_question on a non-existent session returns False without crashing."""
    orch, _client = orch_env
    assert orch.answer_question("no-such-session", "q-id", ["Yes"], "me") is False


def test_answer_question_delegates_to_driver(orch_env):
    """answer_question on a live session delegates to the driver's question registry."""
    orch, client = orch_env

    session = client.portal.call(lambda: orch.create_session("q-test", identity="me"))
    session_id = session.id
    driver = orch.driver(session_id)
    assert driver is not None

    # Register a question directly on the driver's question registry.
    async def _register():
        qid, fut = driver._qreg.request(
            session_id, "t1", "H", "Q?", [{"label": "Option X"}], False, "me"
        )
        return qid, fut

    qid, fut = client.portal.call(_register)

    # answer_question via orchestrator.
    result = orch.answer_question(session_id, qid, ["Option X"], "me")
    assert result is True

    # The future should now be resolved.
    async def _check():
        return fut.done() and await fut

    resolved = client.portal.call(_check)
    assert resolved == ["Option X"]

    client.portal.call(orch.close_session, session_id)


def test_pending_questions_unknown_session_returns_empty(orch_env):
    """pending_questions on a non-existent session returns [] without crashing."""
    orch, _client = orch_env
    assert orch.pending_questions("no-such-session") == []


def test_pending_questions_delegates_to_driver(orch_env):
    """pending_questions returns the full payload for each still-pending question."""
    orch, client = orch_env

    session = client.portal.call(lambda: orch.create_session("q-pending-test", identity="me"))
    session_id = session.id
    driver = orch.driver(session_id)
    assert driver is not None

    opts = [{"label": "Yes"}, {"label": "No"}]

    async def _register():
        qid, fut = driver._qreg.request(
            session_id, "turn-42", "My Header", "Is this right?", opts, True, "me"
        )
        return qid, fut

    qid, fut = client.portal.call(_register)

    pendings = orch.pending_questions(session_id)
    assert len(pendings) == 1
    p = pendings[0]
    assert p["question_id"] == qid
    assert p["turn_id"] == "turn-42"
    assert p["header"] == "My Header"
    assert p["question"] == "Is this right?"
    assert p["options"] == opts
    assert p["multi_select"] is True

    # After resolving, pending_questions must return [].
    orch.answer_question(session_id, qid, ["Yes"], "me")
    assert orch.pending_questions(session_id) == []

    client.portal.call(orch.close_session, session_id)
