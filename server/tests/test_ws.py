"""WebSocket bridge tests (T024).

Uses FastAPI's TestClient (starlette synchronous WS) against an app built with
a real SessionOrchestrator whose CCSWITCH_CLAUDE_BIN points at the fake PTY stub.

Key constraint: PTY fd readers are registered on the TestClient's anyio event loop,
so any coroutine that touches the session manager must be called via
``client.portal.call(coro)`` — not via ``asyncio.new_event_loop()``.

Assertions:
- WS without identity header is rejected
- attach replays scrollback then sends replayComplete
- input produces output echoes from the fake
- interrupt is accepted (no crash)
- on PTY exit the client gets sessionEnded
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
FAKE_PTY = str(REPO_ROOT / "tests" / "fakes" / "fake_claude_pty.py")

_IDENTITY_HEADERS = {"Tailscale-User-Login": "wstest@example.com"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from ccswitch.oauth import Usage
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return Usage(10.0, reset, 10.0, reset, False)


def _seed_account(name: str) -> None:
    import time as _time
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    now_ms = int(_time.time() * 1000)
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok",
        "refreshToken": f"{name}-ref",
        "expiresAt": now_ms + 9_000_000,
        "subscriptionType": "max",
    })
    shared_mcp = paths.shared_mcp_path()
    if not shared_mcp.exists():
        write_json_secure(shared_mcp, {})


def _recv_messages(ws, *, until_type: str, timeout: float = 8.0) -> list[dict]:
    """Collect JSON messages until we see until_type or timeout."""
    msgs: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = ws.receive_text()
            msg = json.loads(raw)
            msgs.append(msg)
            if msg.get("type") == until_type:
                return msgs
        except Exception:
            break
    return msgs


def _recv_type(ws, target_type: str, timeout: float = 8.0) -> dict | None:
    msgs = _recv_messages(ws, until_type=target_type, timeout=timeout)
    for m in msgs:
        if m.get("type") == target_type:
            return m
    return None


def _wait_scrollback(orch: SessionOrchestrator, session_id: str, needle: bytes, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if needle in orch.manager.scrollback(session_id):
                return True
        except KeyError:
            pass
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def ws_setup(tmp_path):
    """Yield (app, orchestrator, TestClient).

    TestClient is already entered as a context manager so:
    - the app lifespan ran (DB initialised, orchestrator started)
    - client.portal is available for running coroutines on the app's event loop
    """
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

        orch = SessionOrchestrator(
            claude_bin=[sys.executable, FAKE_PTY],
            work_dir=str(REPO_ROOT),
            poll_interval=9999.0,
            usage_fn=_fake_usage,
        )
        app = create_app(orchestrator=orch)

        client = TestClient(app, raise_server_exceptions=False)
        with client:
            # client.portal is now set — use it to call async methods
            yield app, orch, client


# ---------------------------------------------------------------------------
# T024-a: WS without identity header is rejected
# ---------------------------------------------------------------------------

def test_ws_no_identity_rejected(ws_setup):
    """A WebSocket upgrade without identity header must be rejected."""
    _app, _orch, client = ws_setup
    rejected = False
    try:
        with client.websocket_connect("/ws") as ws:
            # Server must reject immediately; any receive should fail
            ws.receive_text()
    except Exception:
        rejected = True
    assert rejected, "expected WS without identity to be rejected"


# ---------------------------------------------------------------------------
# T024-b: attach replays scrollback then sends replayComplete
# ---------------------------------------------------------------------------

def test_ws_attach_replays_scrollback_and_replay_complete(ws_setup):
    """Create session via orchestrator, attach via WS, verify output + replayComplete."""
    _app, orch, client = ws_setup

    # Run create_session on the TestClient's event loop so the PTY fd reader
    # is registered on the same loop that drives the WS handler.
    session = client.portal.call(orch.create_session, "test-attach")
    session_id = session.id

    # Wait for banner in scrollback (sync polling — fd reader is on app's loop)
    assert _wait_scrollback(orch, session_id, b"fake-claude ready"), (
        "fake PTY banner did not appear in scrollback"
    )

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))

        msgs = _recv_messages(ws, until_type="replayComplete", timeout=8.0)
        types = [m.get("type") for m in msgs]
        assert "replayComplete" in types, f"replayComplete not in {types}"

        output_msgs = [m for m in msgs if m.get("type") == "output"]
        assert output_msgs, "expected at least one output message during attach"
        combined = b"".join(base64.b64decode(m["data"]) for m in output_msgs)
        assert b"fake-claude ready" in combined, f"banner not in output: {combined!r}"

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# T024-c: input produces output echo from the fake
# ---------------------------------------------------------------------------

def test_ws_input_produces_output(ws_setup):
    """Send input via WS; the fake PTY echoes it back as output."""
    _app, orch, client = ws_setup

    session = client.portal.call(orch.create_session, "test-input")
    session_id = session.id
    assert _wait_scrollback(orch, session_id, b"fake-claude ready"), "banner missing"

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        rc = _recv_type(ws, "replayComplete", timeout=8.0)
        assert rc is not None, "did not get replayComplete"

        payload = base64.b64encode(b"hello world\n").decode()
        ws.send_text(json.dumps({"type": "input", "sessionId": session_id, "data": payload}))

        found_echo = False
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            try:
                raw = ws.receive_text()
                msg = json.loads(raw)
                if msg.get("type") == "output":
                    data = base64.b64decode(msg["data"])
                    if b"hello world" in data or b"turn1" in data:
                        found_echo = True
                        break
            except Exception:
                break

        assert found_echo, "did not receive echo of input from fake PTY"

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# T024-d: interrupt is accepted (no crash)
# ---------------------------------------------------------------------------

def test_ws_interrupt_accepted(ws_setup):
    """Send interrupt message; verify no protocol error is returned.

    Note: the fake PTY exits on SIGINT (it receives \x03 via stdin); the real
    claude binary handles SIGINT gracefully. We only verify that the interrupt
    message is accepted without an error response — not that the process survives.
    """
    _app, orch, client = ws_setup

    session = client.portal.call(orch.create_session, "test-interrupt")
    session_id = session.id
    assert _wait_scrollback(orch, session_id, b"fake-claude ready"), "banner missing"

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _recv_type(ws, "replayComplete", timeout=8.0)

        ws.send_text(json.dumps({"type": "interrupt", "sessionId": session_id}))

        # Collect a few messages; must not see an error response to the interrupt
        msgs: list[dict] = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                raw = ws.receive_text()
                msg = json.loads(raw)
                msgs.append(msg)
                # stop collecting after sessionEnded (fake exits on Ctrl-C)
                if msg.get("type") in ("sessionEnded", "error"):
                    break
            except Exception:
                break

        error_msgs = [m for m in msgs if m.get("type") == "error"]
        assert not error_msgs, f"unexpected error after interrupt: {error_msgs}"


# ---------------------------------------------------------------------------
# T024-e: sessionEnded received when PTY exits via /quit
# ---------------------------------------------------------------------------

def test_ws_session_ended_on_exit(ws_setup):
    """When the PTY exits (/quit), the WS client receives sessionEnded."""
    _app, orch, client = ws_setup

    session = client.portal.call(orch.create_session, "test-exit")
    session_id = session.id
    assert _wait_scrollback(orch, session_id, b"fake-claude ready"), "banner missing"

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        rc = _recv_type(ws, "replayComplete", timeout=8.0)
        assert rc is not None, "did not get replayComplete"

        # Send /quit to cause the fake PTY to exit
        payload = base64.b64encode(b"/quit\n").decode()
        ws.send_text(json.dumps({"type": "input", "sessionId": session_id, "data": payload}))

        ended = _recv_type(ws, "sessionEnded", timeout=10.0)
        assert ended is not None, "did not receive sessionEnded"
        assert ended.get("sessionId") == session_id
