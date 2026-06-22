"""Integration tests: WS prompt frame forwards attachmentIds; create-session carries them.

These are full integration tests that drive the real /ws endpoint and POST /api/sessions
through FastAPI's TestClient, following the harness established in test_ws.py.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from claude_agent_sdk import TextBlock
from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_IDENTITY_HEADERS = {"Tailscale-User-Login": "wstest@example.com"}


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_ws.py)
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
    cls = client_cls or FakeSDKClient

    def make(cwd, config_dir, can_use_tool, mode, resume):
        return cls(list(msgs), on_can_use_tool=can_use_tool)

    return make


def _drain_events(ws, *, until_type: str, timeout: float = 8.0) -> list[dict]:
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
    """Identical to ws_env in test_ws.py — yields a build(msgs) factory."""
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
# Helper: seed an Attachment row + file, returns (id, media_type)
# ---------------------------------------------------------------------------


def _seed_attachment(client, store_home: Path) -> tuple[str, str]:
    """Insert a real Attachment row into the in-memory DB and write a stub file.

    Uses portal.call to run async DB code on the event loop the app is using.
    Returns (attachment_id, media_type).
    """
    import asyncio as _asyncio

    async def _insert():
        from server.db import db_session
        from server.db.models import Attachment
        async with db_session() as db:
            row = Attachment(
                media_type="image/png",
                filename="test.png",
                byte_size=3,
                sha256="abc123",
                uploaded_by="wstest@example.com",
            )
            db.add(row)
            await db.flush()
            rid = row.id
            await db.commit()
        return rid

    rid = client.portal.call(_insert)
    # Write a stub file so resolve_and_bind can find it.
    att_dir = store_home / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    (att_dir / f"{rid}.png").write_bytes(b"PNG")
    return rid, "image/png"


# ---------------------------------------------------------------------------
# Test 1: WS prompt frame forwards attachmentIds → user event carries attachments
# ---------------------------------------------------------------------------


def test_ws_prompt_frame_forwards_attachment_ids(ws_env, tmp_path):
    """A prompt WS frame carrying attachmentIds must forward them through orchestrator.send_prompt,
    producing a user event with the seeded attachment id and mediaType in the batch stream.
    """
    msgs = [
        f.sys_init("sess-att"),
        f.stream_start(uuid="t1"),
        f.stream_text("got it", uuid="t1"),
        f.assistant(TextBlock(text="got it"), uuid="t1"),
        f.result(session_id="sess-att", uuid="t1"),
    ]
    store_home = tmp_path / "store"
    # The ws_env fixture sets CCSWITCH_HOME to store_home; replicate for our tmp_path.
    # Actually ws_env uses its OWN tmp_path — we need to reach into it. Instead, we
    # inject our own ws_env call with a fixture that shares a tmp_path.
    # Workaround: use the ws_env fixture's internal store_home by calling build() and
    # then seeding the attachment via the client portal AFTER build().
    _app, orch, client = ws_env(msgs)

    # Determine where ws_env's store_home lives (CCSWITCH_HOME from the env patch).
    ws_store_home = Path(os.environ["CCSWITCH_HOME"])
    att_id, media_type = _seed_attachment(client, ws_store_home)

    session = client.portal.call(orch.create_session, "test-attach-ids")
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")

        ws.send_text(json.dumps({
            "type": "prompt",
            "sessionId": session_id,
            "text": "look",
            "attachmentIds": [att_id],
        }))
        events = _drain_events(ws, until_type="turnEnd", timeout=8.0)

    types = [e.get("type") for e in events]
    assert "turnEnd" in types, f"no turnEnd in {types}"

    user_events = [e for e in events if e.get("type") == "user"]
    assert user_events, f"no user event in {types}"

    user_ev = user_events[0]
    attachments = user_ev.get("attachments", [])
    assert len(attachments) == 1, f"expected 1 attachment in user event, got {attachments}"
    assert attachments[0]["id"] == att_id, (
        f"attachment id mismatch: expected {att_id}, got {attachments[0].get('id')}"
    )
    assert attachments[0]["mediaType"] == media_type, (
        f"mediaType mismatch: expected {media_type}, got {attachments[0].get('mediaType')}"
    )

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# Test 2: POST /api/sessions with attachmentIds is accepted (201, no error)
# ---------------------------------------------------------------------------


def test_create_session_accepts_attachment_ids(ws_env):
    """POST /api/sessions with attachmentIds must be accepted (201) without error."""
    _app, orch, client = ws_env([])

    resp = client.post(
        "/api/sessions",
        json={"name": "att-session", "attachmentIds": ["fake-id-1", "fake-id-2"]},
        headers=_IDENTITY_HEADERS,
    )
    assert resp.status_code == 201, (
        f"expected 201, got {resp.status_code}: {resp.text}"
    )
    data = resp.json()
    assert "id" in data, f"no id in response: {data}"
    # No error key present.
    assert "error" not in data, f"unexpected error in response: {data}"
