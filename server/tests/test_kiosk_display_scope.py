"""The read-only display (kiosk) principal (#1408): mint → exchange → read-only session.

One file for the whole principal. The client's ``?kiosk=1`` is a layout guard; THIS is
the enforcement — a display credential gets 403 on every mutation and on every socket
whatever URL the page was opened at.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.api.display_projection import project_for_display
from server.app import create_app
from server.assistant.policy import DISPLAY_ROUTE_ALLOW, display_route_allowed

_ENV = {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:", "VIBERPIT_DEV": "1",
        "VIBERPIT_DEV_INSECURE_COOKIE": "1", "VIBERPIT_TAILNET_ALLOWLIST": "",
        "VIBERPIT_SESSION_SECRET": "x" * 32}
_OWNER = {"Tailscale-User-Login": "owner@example.com"}
_SID = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
_FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture
def client():
    with patch.dict(os.environ, _ENV):
        app = create_app()
        with TestClient(app) as c:
            yield c


def _mint(c: TestClient, label: str = "lobby", ttl_days: int = 30) -> dict:
    r = c.post("/api/assistant/tokens", headers=_OWNER,
               json={"scope": "display", "ttlDays": ttl_days, "label": label})
    assert r.status_code == 201, r.text
    return r.json()


def _exchange(c: TestClient, token: str):
    return c.post("/api/kiosk/session", content=f"token={token}", headers=_FORM,
                  follow_redirects=False)


def _display_client(c: TestClient, token: str) -> None:
    r = _exchange(c, token)
    assert r.status_code == 303, r.text


def test_full_identity_mints_display_token_with_label_and_expiry(client):
    tok = _mint(client, label="lobby", ttl_days=7)
    assert tok["scope"] == "display" and tok["label"] == "lobby"
    assert tok["token"].startswith("ccas2:")
    exp = datetime.fromisoformat(tok["expiresAt"])
    assert exp > datetime.now(timezone.utc).replace(tzinfo=exp.tzinfo) + timedelta(days=6)


def test_exchange_sets_display_cookie_and_strips_token_from_location(client):
    tok = _mint(client)
    r = _exchange(client, tok["token"])
    assert r.status_code == 303
    assert "token" not in r.headers["location"]
    assert "viberpit_display=" in r.headers["set-cookie"]
    assert "HttpOnly" in r.headers["set-cookie"]
    assert tok["token"] not in r.headers["set-cookie"]


def test_display_token_is_exchange_only_not_a_bearer(client):
    tok = _mint(client)
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok['token']}"})
    assert r.status_code == 401


def test_exchange_rejects_a_non_display_token(client):
    r = client.post("/api/assistant/tokens", headers=_OWNER,
                    json={"scope": "act", "ttlDays": 1})
    assert _exchange(client, r.json()["token"]).status_code == 401


def test_exchange_garbage_and_missing_token_401_no_cookie(client):
    for body in ("token=ccas2:nope", "", "other=1"):
        r = client.post("/api/kiosk/session", content=body, headers=_FORM,
                        follow_redirects=False)
        assert r.status_code == 401
        assert "set-cookie" not in r.headers


def test_exchange_from_disallowed_origin_403(client):
    tok = _mint(client)
    r = client.post("/api/kiosk/session", content=f"token={tok['token']}",
                    headers={**_FORM, "Origin": "https://evil.example"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_display_session_reads_feed_and_me_hides_the_minting_operator(client):
    _display_client(client, _mint(client, label="lobby")["token"])
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body == {"identity": "display:lobby", "scope": "display", "authMode": "display"}
    assert "owner@example.com" not in json.dumps(body)
    assert client.get("/api/pipeline/snapshot").status_code == 200
    assert "repoRoot" in client.get("/api/config").json()
    assert client.get("/api/config").json()["repoRoot"] is None


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/sessions"),
    ("POST", f"/api/sessions/{_SID}/prompt"),
    ("DELETE", f"/api/sessions/{_SID}"),
    ("PATCH", "/api/automations/backlog-runner"),
    ("PUT", "/api/notifications/quiet-hours"),
    ("POST", "/api/pipeline/pr/ready-merge"),
    ("POST", "/api/assistant/tokens"),
    ("GET", "/api/assistant/tokens"),
    ("GET", "/api/accounts"),
    ("GET", "/api/sessions"),
    ("GET", "/api/settings"),
])
def test_display_session_is_refused_everything_outside_the_allowlist(client, method, path):
    _display_client(client, _mint(client)["token"])
    r = client.request(method, path, json={})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "DISPLAY_SCOPE_FORBIDDEN"


def test_display_allowlist_rows_are_reads_except_logout():
    writes = [(m, rx.pattern) for m, rx in DISPLAY_ROUTE_ALLOW if m != "GET"]
    assert writes == [("POST", "/api/auth/logout$")]
    assert not display_route_allowed("GET", "/api/sessions")
    assert not display_route_allowed("POST", "/index.html")
    assert display_route_allowed("GET", "/assets/app.js")


def test_display_cookie_refused_on_all_sockets(client):
    _display_client(client, _mint(client)["token"])
    for path in ("/ws", "/ws/vnc?session=x", "/ws/stt"):
        try:
            with client.websocket_connect(path, headers=_OWNER) as ws:
                ws.receive_text()
            closed = False
        except Exception:
            closed = True
        assert closed, path


def test_revoking_the_token_ends_the_display_on_its_next_request(client):
    tok = _mint(client)
    _display_client(client, tok["token"])
    assert client.get("/api/pipeline/snapshot").status_code == 200
    # The owner revokes from another browser (the display's jar holds only its cookie).
    owner = dict(client.cookies)
    client.cookies.clear()
    assert client.delete(f"/api/assistant/tokens/{tok['id']}",
                         headers=_OWNER).status_code == 200
    client.cookies.update(owner)
    r = client.get("/api/pipeline/snapshot")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "DISPLAY_SESSION_ENDED"
    client.cookies.clear()
    assert _exchange(client, tok["token"]).status_code == 401


def test_display_cookie_overrides_full_ambient_identity(client):
    _display_client(client, _mint(client)["token"])
    # Same browser also presents the operator's Tailscale identity: narrower wins.
    r = client.post("/api/sessions", headers=_OWNER, json={})
    assert r.status_code == 403
    assert client.get("/api/auth/me", headers=_OWNER).json()["scope"] == "display"


def test_logout_ends_only_the_display_session(client):
    tok = _mint(client)
    _display_client(client, tok["token"])
    assert client.post("/api/auth/logout").status_code == 204
    client.cookies.clear()
    assert _exchange(client, tok["token"]).status_code == 303


def test_audit_never_contains_the_token(client):
    tok = _mint(client)
    _exchange(client, tok["token"])
    _exchange(client, tok["token"] + "x")

    async def _rows():
        from sqlalchemy import select

        from server.db import db_session
        from server.db.models import AuditEvent
        async with db_session() as db:
            return [json.dumps([r.type, r.identity, r.detail]) for r in
                    (await db.execute(select(AuditEvent))).scalars().all()]
    rows = client.portal.call(_rows)
    blob = "\n".join(rows)
    assert tok["token"] not in blob
    assert "kiosk.exchange" in blob and "kiosk.exchange.deny" in blob


@pytest.mark.parametrize("token", ["ccas1:abc.DEF_-123", "ccas2:abc.DEF_-123"])
def test_ccas_tokens_are_redacted(token):
    from server.audit import redact_detail
    assert token not in json.dumps(redact_detail({"msg": f"got {token} here"}))


def test_projection_removes_identity_goal_and_hostname():
    frame = {"repos": [{"name": "acme/app", "goal": "ship the secret thing",
                        "identity": "owner@example.com", "host": "dobby.tail1234.ts.net",
                        "note": "run by owner@example.com on rocky.tail1234.ts.net",
                        "lanes": [{"state": "green", "prompt": "do x"}]}], "n": 3}
    out = json.dumps(project_for_display(frame))
    assert not re.search(r"@|\.ts\.net|secret thing|do x", out)
    assert '"green"' in out and '"acme/app"' in out and '"n": 3' in out
