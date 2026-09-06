"""`Bearer ccas2:…` accepted on the `/ws` upgrade (spec v2 Task 9.7, `ccswitch ask`).

`BaseHTTPMiddleware` never runs on a WebSocket upgrade (`server/tests/test_assistant_external_token.py`
covers the HTTP half of the same token), so `require_ws_identity` is its own enforcement point and
gets its own test module. Uses the same mint-and-insert pattern as the HTTP tests, plus `test_ws.py`'s
`ws_env` fixture and `_PermissionFakeClient` for the one round-trip test that needs a live permission
card to check the `via="cli"` audit tag.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.assistant.token import mint_user_token
from server.tests.test_ws import _IDENTITY_HEADERS, _PermissionFakeClient, ws_env  # noqa: F401

_ENV = {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:", "CCSWITCH_DEV": "1",
        "CCSWITCH_TAILNET_ALLOWLIST": "", "CCSWITCH_SESSION_SECRET": "x" * 32,
        "CCSWITCH_ASSISTANT_EXTERNAL_TOKENS": "true"}


def _client():
    with patch.dict(os.environ, _ENV):
        app = create_app()
    return TestClient(app)


def _mint_and_insert(c: TestClient, identity: str, scope: str, **kw) -> str:
    """Mirrors test_assistant_external_token.py's helper of the same name — mints a
    ccas2 token and inserts its DB row via the app's own event loop/connection."""
    import uuid
    token_id = str(uuid.uuid4())
    token = mint_user_token(identity, scope, token_id)

    async def _insert() -> None:
        from server.db import db_session
        from server.db.models import AssistantToken
        now = datetime.now(timezone.utc)
        async with db_session() as db:
            db.add(AssistantToken(
                id=token_id, identity=identity, scope=scope,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=now + timedelta(seconds=kw.get("expires_in_s", 3600)),
                revoked_at=now if kw.get("revoked") else None,
            ))
            await db.commit()

    assert c.portal is not None
    c.portal.call(_insert)
    return token


def _closed_before_accept(c: TestClient, headers: dict) -> bool:
    """True if the socket never got past `require_ws_identity` — mirrors
    `test_ws_no_identity_rejected`'s own try/except shape, since a pre-accept close
    surfaces as *some* exception on either `__enter__` or the first receive,
    depending on the ASGI transport's buffering."""
    try:
        with c.websocket_connect("/ws", headers=headers) as ws:
            ws.receive_text()
        return False
    except Exception:
        return True


def test_valid_act_scope_token_opens_the_socket():
    with patch.dict(os.environ, _ENV):
        with _client() as c:
            tok = _mint_and_insert(c, "u@example.com", "act")
            with c.websocket_connect(
                "/ws", headers={"Authorization": f"Bearer {tok}"}
            ) as ws:
                # Accepted: attaching to a made-up session gets an ERROR FRAME back,
                # not a socket close — proving the upgrade itself succeeded.
                ws.send_text(json.dumps({"type": "attach", "sessionId": "nope-nope"}))
                raw = ws.receive_text()
                msg = json.loads(raw)
                assert msg.get("type") in ("error", "batch", "replayComplete")


def test_read_scope_token_is_rejected():
    with patch.dict(os.environ, _ENV):
        with _client() as c:
            tok = _mint_and_insert(c, "u@example.com", "read")
            assert _closed_before_accept(
                c, {"Authorization": f"Bearer {tok}"})


def test_revoked_token_is_rejected():
    with patch.dict(os.environ, _ENV):
        with _client() as c:
            tok = _mint_and_insert(c, "u@example.com", "act", revoked=True)
            assert _closed_before_accept(
                c, {"Authorization": f"Bearer {tok}"})


def test_expired_token_is_rejected():
    with patch.dict(os.environ, _ENV):
        with _client() as c:
            tok = _mint_and_insert(c, "u@example.com", "act", expires_in_s=-10)
            assert _closed_before_accept(
                c, {"Authorization": f"Bearer {tok}"})


def test_unknown_token_id_is_rejected():
    with patch.dict(os.environ, _ENV):
        with _client() as c:
            tok = mint_user_token("u@example.com", "act", "never-inserted")
            assert _closed_before_accept(
                c, {"Authorization": f"Bearer {tok}"})


def test_feature_flag_off_rejects_token():
    env = dict(_ENV)
    env["CCSWITCH_ASSISTANT_EXTERNAL_TOKENS"] = "false"
    with patch.dict(os.environ, env):
        with _client() as c:
            tok = _mint_and_insert(c, "u@example.com", "act")
            assert _closed_before_accept(
                c, {"Authorization": f"Bearer {tok}"})


def test_tailscale_identity_path_is_unaffected():
    """The ordinary browser path (no Authorization header) still works — extending
    require_ws_identity for ccas2 must not disturb the existing fallback."""
    with patch.dict(os.environ, _ENV):
        with _client() as c:
            with c.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                ws.send_text(json.dumps({"type": "attach", "sessionId": "nope-nope"}))
                raw = ws.receive_text()
                assert json.loads(raw).get("type") in ("error", "batch", "replayComplete")


def test_permission_answered_over_ccas2_socket_is_audited_via_cli(ws_env, monkeypatch):  # noqa: F811 — pytest fixture
    monkeypatch.setenv("CCSWITCH_ASSISTANT_EXTERNAL_TOKENS", "true")
    _app, orch, client = ws_env([], client_cls=_PermissionFakeClient)
    session = client.portal.call(
        lambda: orch.create_session("test-cli-perm", identity="cli-user@example.com")
    )
    session_id = session.id
    tok = _mint_and_insert(client, "cli-user@example.com", "act")

    with client.websocket_connect(
        "/ws", headers={"Authorization": f"Bearer {tok}"}
    ) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")

        ws.send_text(json.dumps({
            "type": "prompt", "sessionId": session_id, "text": "go",
            "context": {"route": "/cli"},
        }))
        events = _drain_events(ws, until_type="permissionRequest", timeout=8.0)
        req = next(e for e in events if e.get("type") == "permissionRequest")

        ws.send_text(json.dumps({
            "type": "permissionResponse", "sessionId": session_id,
            "permId": req["permId"], "decision": "allow",
        }))
        _drain_events(ws, until_type="turnEnd", timeout=8.0)

    async def _read_audit():
        from sqlalchemy import select
        from server.db import db_session
        from server.db.models import AuditEvent
        async with db_session() as db:
            rows = (await db.execute(
                select(AuditEvent).where(AuditEvent.type == "permission.allow")
            )).scalars().all()
        return rows

    rows = client.portal.call(_read_audit)
    assert rows and rows[-1].via == "cli"

    client.portal.call(orch.close_session, session_id)


def _drain_events(ws, *, until_type: str, timeout: float = 8.0) -> list[dict]:
    """Local copy of test_ws.py's helper — not imported, since importing a bare name
    for one call site would obscure where the receive-loop semantics actually live."""
    import time
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
