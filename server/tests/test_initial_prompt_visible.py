"""Regression test for issue #4: initial prompt turn must be visible on first attach.

When a session is created with an initial_prompt, the prompt is deferred until the
first WebSocket client attaches.  This guarantees visibility: the events appear live
in front of the watching client rather than racing the transcript-file write or
arriving as buffered events before any consumer exists.

Scenario tested:
  create_session("name", initial_prompt="Hello!") [no initial run, prompt deferred]
  → client attaches
  → server sends prompt to Claude
  → user echo + assistant response stream to the client live
"""
from __future__ import annotations

import json
import os
import time
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


REPO_ROOT = Path(__file__).parent.parent.parent
_IDENTITY_HEADERS = {"Tailscale-User-Login": "wstest@example.com"}


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


def _drain_events(ws, *, until_type: str, timeout: float = 8.0) -> list[dict]:
    """Receive frames and flatten batch frames into a list of event dicts."""
    events: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = ws.receive_text()
        except Exception:
            break
        msg = json.loads(raw)
        if msg.get("type") == "batch":
            events.extend(msg.get("events", []))
        else:
            events.append(msg)
        if any(e.get("type") == until_type for e in events):
            return events
    return events


@pytest.fixture()
def ws_env_ip(tmp_path):
    """Fixture matching the ws_env pattern but for initial-prompt tests."""
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

        def build(msgs, client_cls=None):
            cls = client_cls or FakeSDKClient

            def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *, on_pre_compact=None):
                return cls(list(msgs), on_can_use_tool=can_use_tool)

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


def test_initial_prompt_deferred_until_first_attach(ws_env_ip):
    """The initial prompt must NOT be sent during create_session; it must be sent
    only after the first WebSocket client attaches (deferred execution).

    Before the fix: the prompt was sent immediately inside create_session, so
    events were emitted to driver._out before any consumer existed, and the
    transcript file might not yet be flushed on attach — leaving the first turn
    invisible.
    """
    msgs = [
        f.sys_init("sess-ip1"),
        f.stream_start(uuid="t1"),
        f.stream_text("Hello from initial prompt", uuid="t1"),
        f.assistant(TextBlock(text="Hello from initial prompt"), uuid="t1"),
        f.result(session_id="sess-ip1", uuid="t1"),
    ]
    _app, orch, client = ws_env_ip(msgs)

    # Create session WITH an initial prompt.
    session = client.portal.call(
        lambda: orch.create_session("test-initial-prompt", initial_prompt="Hello!")
    )
    session_id = session.id

    # --- Verify the prompt was NOT sent immediately during create_session ---
    # After create_session returns, the driver should have sent NO prompts yet
    # (pending initial prompt is stored but deferred until first attach).
    driver = orch.driver(session_id)
    assert driver is not None
    # The driver's underlying SDK client tracks sent prompts in .sent
    assert len(driver._client.sent) == 0, (
        f"initial prompt was sent immediately during create_session "
        f"(driver._client.sent={driver._client.sent!r}); "
        "it must be deferred until first attach"
    )

    # --- Verify the pending initial prompt is stored in the orchestrator ---
    pending = orch.pop_pending_initial_prompt(session_id)
    assert pending == "Hello!", (
        f"expected pending initial prompt 'Hello!', got {pending!r}"
    )
    # Restore it (pop consumed it; we need it back for the attach test below)
    # We test pop then re-create, but actually let's use a fresh session for the full flow.

    # --- Full flow test: initial prompt is sent on first attach and events are delivered ---
    session2 = client.portal.call(
        lambda: orch.create_session("test-ip-flow", initial_prompt="Hello again!")
    )
    session_id2 = session2.id
    driver2 = orch.driver(session_id2)
    assert driver2 is not None
    assert len(driver2._client.sent) == 0, "prompt must not be sent before first attach"

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id2}))

        # After attach, the pending initial prompt should have been sent.
        # Drain through the *whole* first turn in ONE pass, to its terminal
        # event (turnEnd). All session frames now ride a single outbound FIFO,
        # so replayComplete and the first turn's events can share one batch —
        # a second drain "from replayComplete to turnEnd" would re-wait for a
        # turnEnd that was already consumed and block forever.
        all_events: list[dict] = _drain_events(ws, until_type="turnEnd", timeout=10.0)

        types = [e.get("type") for e in all_events]

        # The user echo for the initial prompt must be present
        user_events = [e for e in all_events if e.get("type") == "user"]
        assert len(user_events) >= 1, (
            f"initial prompt user echo not found after first attach; "
            f"got event types: {types}"
        )
        assert any(e.get("text") == "Hello again!" for e in user_events), (
            f"user echo text 'Hello again!' missing; user events: {user_events}"
        )

        # The assistant response must also be present
        assert "turnStart" in types or "textDelta" in types, (
            f"assistant response events missing; got: {types}"
        )

    client.portal.call(orch.close_session, session_id)
    client.portal.call(orch.close_session, session_id2)


def test_initial_prompt_sent_exactly_once_across_two_attaches(ws_env_ip):
    """Regression test for issue #4: a second attach (simulated reconnect) must NOT
    re-send the initial prompt.

    The deferred initial prompt is consumed by pop_pending_initial_prompt on the
    FIRST attach.  A second attach should find no pending prompt (the entry was
    already popped) and therefore the SDK must have received the prompt exactly once
    across both attaches combined.
    """
    msgs = [
        f.sys_init("sess-ip2"),
        f.stream_start(uuid="u1"),
        f.stream_text("Hi from initial", uuid="u1"),
        f.assistant(TextBlock(text="Hi from initial"), uuid="u1"),
        f.result(session_id="sess-ip2", uuid="u1"),
    ]
    _app, orch, client = ws_env_ip(msgs)

    session = client.portal.call(
        lambda: orch.create_session("ip-once-test", initial_prompt="Ping!")
    )
    session_id = session.id

    # First attach — should consume and send the deferred initial prompt.
    # Single drain to the user echo (replayComplete precedes it, possibly in the
    # SAME batch now that all session frames share one FIFO — a second drain
    # would block on an already-consumed frame).
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="user", timeout=10.0)

    # Verify the pending slot is now empty (consumed on first attach).
    assert orch.pop_pending_initial_prompt(session_id) is None, (
        "pending initial prompt was not cleared after first attach"
    )

    # Capture how many times the SDK was called after the first attach.
    driver = orch.driver(session_id)
    assert driver is not None
    sent_after_first = len(driver._client.sent)
    assert sent_after_first >= 1, "SDK should have received the initial prompt on first attach"

    # Second attach — simulates a client reconnect.
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws2:
        ws2.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws2, until_type="replayComplete", timeout=5.0)

    # No additional SDK calls should have been made (prompt was NOT re-sent).
    sent_after_second = len(driver._client.sent)
    assert sent_after_second == sent_after_first, (
        f"initial prompt was re-sent on second attach: "
        f"SDK call count went from {sent_after_first} to {sent_after_second}"
    )

    client.portal.call(orch.close_session, session_id)
