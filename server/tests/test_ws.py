"""WebSocket bridge tests — normalized-event transport.

Uses FastAPI's TestClient (starlette synchronous WS) against an app built with
a real SessionOrchestrator whose SDK factory builds a FakeSDKClient (no real
`claude`, no quota).  The WS bridge ships normalized events as ``batch`` frames.

Assertions:
- WS without identity header is rejected
- attach sends replayComplete
- prompt streams turnStart/textDelta/turnEnd through batch frames
- a tool needing permission emits a permissionRequest; permissionResponse lets it proceed
- an inbound frame larger than 256KB is rejected with an error frame, socket stays open
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from claude_agent_sdk import TextBlock
from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
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


def _factory(msgs, client_cls=None):
    """Return an sdk_factory building a fake client over *msgs*."""
    cls = client_cls or FakeSDKClient

    def make(cwd, config_dir, can_use_tool, mode, resume):
        return cls(list(msgs), on_can_use_tool=can_use_tool)

    return make


def _drain_events(ws, *, until_type: str, timeout: float = 8.0) -> list[dict]:
    """Receive frames and flatten batch frames into a list of event dicts.

    Stops once an event of *until_type* is seen (or timeout).
    """
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


def _make_orch(msgs, *, client_cls=None) -> SessionOrchestrator:
    return SessionOrchestrator(
        sdk_factory=_factory(msgs, client_cls=client_cls),
        work_dir=str(REPO_ROOT),
        poll_interval=9999.0,
        usage_fn=_fake_usage,
    )


@pytest.fixture()
def ws_env(tmp_path):
    """Yield a factory ``build(msgs, client_cls=None) -> (app, orch, client)``.

    The returned TestClient must be entered with ``with client:`` to run the
    lifespan; the helper does this and yields the entered client.
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

    created: list[TestClient] = []

    with env_patch, usage_patch, compose_patch, select_patch:
        _seed_account("alpha")

        def build(msgs, client_cls=None):
            orch = _make_orch(msgs, client_cls=client_cls)
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


# ---------------------------------------------------------------------------
# WS without identity header is rejected
# ---------------------------------------------------------------------------

def test_ws_no_identity_rejected(ws_env):
    _app, _orch, client = ws_env([])
    rejected = False
    try:
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()
    except Exception:
        rejected = True
    assert rejected, "expected WS without identity to be rejected"


# ---------------------------------------------------------------------------
# replayComplete is sent on attach
# ---------------------------------------------------------------------------

def test_replayComplete_sent_on_attach(ws_env):
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "test-replay")
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        events = _drain_events(ws, until_type="replayComplete")
        assert any(e.get("type") == "replayComplete" for e in events), (
            f"replayComplete not in {[e.get('type') for e in events]}"
        )

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# attach + prompt streams the turn live
# ---------------------------------------------------------------------------

def test_attach_replays_then_live(ws_env):
    msgs = [
        f.sys_init("sess-1"),
        f.stream_start(uuid="t1"),
        f.stream_text("Hello ", uuid="t1"),
        f.stream_text("there", uuid="t1"),
        f.assistant(TextBlock(text="Hello there"), uuid="t1"),
        f.result(session_id="sess-1", uuid="t1"),
    ]
    _app, orch, client = ws_env(msgs)
    session = client.portal.call(orch.create_session, "test-live")
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        # consume up to replayComplete first
        _drain_events(ws, until_type="replayComplete")

        ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "hi"}))
        events = _drain_events(ws, until_type="turnEnd", timeout=8.0)
        types = [e.get("type") for e in events]
        assert "turnStart" in types, f"no turnStart in {types}"
        assert "textDelta" in types, f"no textDelta in {types}"
        assert "turnEnd" in types, f"no turnEnd in {types}"
        text = "".join(e.get("text", "") for e in events if e.get("type") == "textDelta")
        assert "Hello there" in text, f"unexpected text: {text!r}"

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# Permission round-trip: tool needs permission, allow lets it proceed
# ---------------------------------------------------------------------------

class _PermissionFakeClient(FakeSDKClient):
    """Fake client that invokes can_use_tool before streaming, simulating a
    tool that requires interactive approval, then proceeds once approved."""

    async def receive_response(self):
        # First message announces the turn.
        yield f.stream_start(uuid="t1")
        # Ask for permission for a Bash tool — blocks until resolve/deny.
        if self.on_can_use_tool is not None:
            await self.on_can_use_tool("Bash", {"command": "ls"}, None)
        if self.interrupted:
            return
        # Approved → finish the turn.
        yield f.stream_text("done", uuid="t1")
        yield f.result(session_id="sess-1", uuid="t1")


def test_permission_round_trip(ws_env):
    _app, orch, client = ws_env([], client_cls=_PermissionFakeClient)
    # The permission must be requested AND resolved under the same identity that
    # the WS connection presents, so create the session with the WS identity.
    session = client.portal.call(
        lambda: orch.create_session("test-perm", identity="wstest@example.com")
    )
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")

        ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "go"}))
        events = _drain_events(ws, until_type="permissionRequest", timeout=8.0)
        req = next((e for e in events if e.get("type") == "permissionRequest"), None)
        assert req is not None, f"no permissionRequest in {[e.get('type') for e in events]}"
        assert req.get("tool") == "Bash"
        perm_id = req["permId"]

        ws.send_text(json.dumps({
            "type": "permissionResponse", "sessionId": session_id,
            "permId": perm_id, "decision": "allow",
        }))

        events = _drain_events(ws, until_type="turnEnd", timeout=8.0)
        types = [e.get("type") for e in events]
        assert "turnEnd" in types, f"turn did not proceed after allow: {types}"

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# An unknown permId response and interrupt are handled safely
# ---------------------------------------------------------------------------

def test_unknown_permission_and_interrupt_safe(ws_env):
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "test-safe")
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")

        # Unknown permId — must not crash, no error frame expected.
        ws.send_text(json.dumps({
            "type": "permissionResponse", "sessionId": session_id,
            "permId": "does-not-exist", "decision": "allow",
        }))
        # interrupt is accepted.
        ws.send_text(json.dumps({"type": "interrupt", "sessionId": session_id}))

        # Send a benign unknown frame and confirm we get a well-formed error back.
        ws.send_text(json.dumps({"type": "bogus", "sessionId": session_id}))
        events = _drain_events(ws, until_type="error", timeout=4.0)
        err = next((e for e in events if e.get("type") == "error"), None)
        assert err is not None and err.get("code") == "UNKNOWN_TYPE"

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# Frame larger than 256KB is rejected, socket stays open
# ---------------------------------------------------------------------------

def test_frame_too_large_rejected(ws_env):
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "test-big")
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")

        big_text = "x" * 300_000
        ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": big_text}))

        events = _drain_events(ws, until_type="error", timeout=4.0)
        err = next((e for e in events if e.get("type") == "error"), None)
        assert err is not None and err.get("code") == "frame_too_large", (
            f"expected frame_too_large error, got {[e.get('type') for e in events]}"
        )

        # Socket still open: a normal unknown frame still elicits a response.
        ws.send_text(json.dumps({"type": "bogus", "sessionId": session_id}))
        events = _drain_events(ws, until_type="error", timeout=4.0)
        assert any(e.get("code") == "UNKNOWN_TYPE" for e in events), (
            "socket appears closed after frame_too_large"
        )

    client.portal.call(orch.close_session, session_id)
