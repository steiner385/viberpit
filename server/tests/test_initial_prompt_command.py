"""A Claude Code slash command typed as the INITIAL prompt (the dispatch panel)
must execute through the same command-dispatch path the interactive composer
uses — not get sent to the model as a literal prompt turn.

Companion to test_initial_prompt_visible.py (normal prompts) and
test_ws_commands.py (interactive command frames). Shares the ws_env_ip harness
shape from test_initial_prompt_visible.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from claude_agent_sdk import TextBlock

from ccswitch.selector import Picked
from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f
from server.tests.test_initial_prompt_visible import (
    _drain_events,
    _seed_account,
    _IDENTITY_HEADERS,
)

REPO_ROOT = Path(__file__).parent.parent.parent


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from ccswitch.oauth import Usage
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return Usage(10.0, reset, 10.0, reset, False)


@pytest.fixture()
def ws_env_ip(tmp_path):
    store_home = tmp_path / "store"; store_home.mkdir()
    shared_base = tmp_path / "dot-claude"; shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"; cfg_dir.mkdir(parents=True)

    env_patch = patch.dict(os.environ, {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    })
    compose_patch = patch("server.sessions.orchestrator.compose", return_value=cfg_dir)
    trust_patch = patch("server.sessions.orchestrator.configdir.trust_project")
    select_patch = patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha"))

    created: list[TestClient] = []

    with env_patch, compose_patch, trust_patch, select_patch:
        _seed_account("alpha")

        def build(msgs):
            def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *, on_pre_compact=None):
                return FakeSDKClient(list(msgs), on_can_use_tool=can_use_tool)

            orch = SessionOrchestrator(
                sdk_factory=make,
                work_dir=str(REPO_ROOT),
                poll_interval=9999.0,
                usage_fn=_fake_usage,
            )
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)
            client.__enter__()
            created.append(client)
            return app, orch, client

        try:
            yield build
        finally:
            for c in created:
                try:
                    c.__exit__(None, None, None)
                except Exception:
                    pass


def test_slash_command_initial_prompt_executes_as_command(ws_env_ip):
    """`/model sonnet` as the initial prompt applies + persists the model and is
    NOT sent to the model as a literal prompt turn."""
    # A full turn is present so the OLD behavior (send as prompt) would stream a
    # user echo — making the RED failure land on the assertions, not a hang.
    msgs = [
        f.sys_init("sess-cmd1"),
        f.stream_start(uuid="t1"),
        f.stream_text("hi", uuid="t1"),
        f.assistant(TextBlock(text="hi"), uuid="t1"),
        f.result(session_id="sess-cmd1", uuid="t1"),
    ]
    _app, orch, client = ws_env_ip(msgs)

    session = client.portal.call(
        lambda: orch.create_session("cmd-initial", initial_prompt="/model sonnet")
    )
    sid = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
        events = _drain_events(ws, until_type="commandResult", timeout=10.0)

    driver = orch.driver(sid)
    assert driver is not None
    # The command executed: model applied on the driver, nothing queried as a prompt.
    assert driver._client.model == "sonnet"
    assert driver._client.sent == []
    # The client received the same commandResult frame the interactive path returns.
    assert any(e.get("type") == "commandResult" for e in events), (
        f"no commandResult frame; got {[e.get('type') for e in events]}"
    )
    # The raw command text was never echoed as a user prompt.
    assert not any(
        e.get("type") == "user" and str(e.get("text", "")).startswith("/model")
        for e in events
    )

    client.portal.call(orch.close_session, sid)


def test_unknown_slash_initial_prompt_falls_back_to_prompt(ws_env_ip):
    """A leading-slash string that is NOT a known command is sent as a normal
    prompt (mirrors the composer's unknown-command fallback)."""
    msgs = [
        f.sys_init("sess-unk"),
        f.stream_start(uuid="u1"),
        f.stream_text("ok", uuid="u1"),
        f.assistant(TextBlock(text="ok"), uuid="u1"),
        f.result(session_id="sess-unk", uuid="u1"),
    ]
    _app, orch, client = ws_env_ip(msgs)

    session = client.portal.call(
        lambda: orch.create_session("unk-cmd", initial_prompt="/notacommand do stuff")
    )
    sid = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
        events = _drain_events(ws, until_type="turnEnd", timeout=10.0)

    driver = orch.driver(sid)
    assert driver is not None
    # Fell back to a normal prompt: raw text queried and echoed to the client.
    assert driver._client.sent == ["/notacommand do stuff"]
    assert any(
        e.get("type") == "user" and e.get("text") == "/notacommand do stuff"
        for e in events
    ), f"user echo missing; got {[e.get('type') for e in events]}"

    client.portal.call(orch.close_session, sid)
