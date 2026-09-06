from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.assistant.token import UserClaims, mint_user_token, verify_user_token

_ENV = {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:", "CCSWITCH_DEV": "1",
        "CCSWITCH_TAILNET_ALLOWLIST": "", "CCSWITCH_SESSION_SECRET": "x" * 32,
        "CCSWITCH_ASSISTANT_EXTERNAL_TOKENS": "true"}


def test_mint_verify_roundtrip():
    with patch.dict(os.environ, _ENV):
        tok = mint_user_token("u@example.com", "act", "tok-1")
        assert tok.startswith("ccas2:")
        assert verify_user_token(tok, max_age_s=60) == UserClaims("u@example.com", "act", "tok-1")


def test_verify_rejects_tamper_and_expiry():
    with patch.dict(os.environ, _ENV):
        tok = mint_user_token("u@example.com", "act", "tok-1")
        assert verify_user_token(tok[:-2] + "zz", max_age_s=60) is None
        assert verify_user_token("nope", max_age_s=60) is None
        assert verify_user_token(tok, max_age_s=-1) is None


def _client():
    with patch.dict(os.environ, _ENV):
        app = create_app()
    return TestClient(app)


def _mint_and_insert(c: TestClient, identity: str, scope: str, **kw) -> str:
    """Mint a ccas2 token bound to a fresh id and insert its DB row via the app's own
    event loop/connection (a bare `asyncio.run` would open an unrelated in-memory DB)."""
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


def test_valid_read_scope_allows_get_but_not_post():
    with patch.dict(os.environ, _ENV):
        with _client() as c:
            tok = _mint_and_insert(c, "u@example.com", "read")
            r = c.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 200
            assert r.json()["identity"] == "u@example.com"

            r2 = c.post("/api/automations/schedule/preview",
                       json={"schedule": {"cron": "0 8 * * *"}},
                       headers={"Authorization": f"Bearer {tok}"})
            assert r2.status_code == 403
            assert r2.json()["error"]["code"] == "ASSISTANT_ROUTE_FORBIDDEN"


def test_act_scope_allows_post():
    with patch.dict(os.environ, _ENV):
        with _client() as c:
            tok = _mint_and_insert(c, "u@example.com", "act")
            r = c.post("/api/automations/schedule/preview",
                      json={"schedule": {"cron": "0 8 * * *"}},
                      headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 200


def test_revoked_token_returns_401():
    with patch.dict(os.environ, _ENV):
        with _client() as c:
            tok = _mint_and_insert(c, "u@example.com", "act", revoked=True)
            r = c.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 401


def test_expired_token_returns_401():
    with patch.dict(os.environ, _ENV):
        with _client() as c:
            tok = _mint_and_insert(c, "u@example.com", "act", expires_in_s=-10)
            r = c.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 401


def test_unknown_token_id_returns_401():
    with patch.dict(os.environ, _ENV):
        with _client() as c:
            # Signed and structurally valid, but no DB row was ever inserted for it.
            tok = mint_user_token("u@example.com", "act", "never-inserted")
            r = c.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 401


def test_feature_flag_off_returns_401():
    env = dict(_ENV)
    env["CCSWITCH_ASSISTANT_EXTERNAL_TOKENS"] = "false"
    with patch.dict(os.environ, env):
        with _client() as c:
            tok = _mint_and_insert(c, "u@example.com", "act")
            r = c.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 401


def test_operator_route_always_forbidden_for_user_token():
    with patch.dict(os.environ, _ENV):
        with _client() as c:
            tok = _mint_and_insert(c, "u@example.com", "act")
            r = c.post("/api/deploy/start", json={},
                      headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 403


def test_works_from_a_non_loopback_peer():
    with patch.dict(os.environ, _ENV):
        with _client() as c:
            tok = _mint_and_insert(c, "u@example.com", "act")
            far = TestClient(c.app, client=("203.0.113.9", 5000))
            r = far.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 200
            assert r.json()["identity"] == "u@example.com"


def test_create_token_via_bearer_forbidden():
    """POST /api/assistant/tokens is unreachable via any bearer token (ccas1 or
    ccas2) — it isn't in ASSISTANT_ROUTE_ALLOW, so the middleware's own gate 403s it
    before the endpoint's `via` check ever runs."""
    with patch.dict(os.environ, _ENV):
        with _client() as c:
            tok = _mint_and_insert(c, "u@example.com", "act")
            r = c.post("/api/assistant/tokens", json={"scope": "act", "ttlDays": 30},
                      headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 403


def test_mint_list_revoke_via_browser_identity():
    with patch.dict(os.environ, _ENV):
        with _client() as c:
            headers = {"Tailscale-User-Login": "u@example.com"}
            r = c.post("/api/assistant/tokens",
                      json={"scope": "act", "ttlDays": 7, "label": "laptop"},
                      headers=headers)
            assert r.status_code == 201
            body = r.json()
            assert body["token"].startswith("ccas2:")
            token_id, token = body["id"], body["token"]

            # The minted token works against a real allowed route.
            r2 = c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
            assert r2.status_code == 200

            # Listing never exposes the token/hash.
            r3 = c.get("/api/assistant/tokens", headers=headers)
            assert r3.status_code == 200
            listed = r3.json()
            assert len(listed) == 1
            assert listed[0]["id"] == token_id
            assert "token" not in listed[0] and "tokenHash" not in listed[0]

            # Revoking it retires the token immediately.
            r4 = c.delete(f"/api/assistant/tokens/{token_id}", headers=headers)
            assert r4.status_code == 200
            r5 = c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
            assert r5.status_code == 401


def test_create_token_invalid_scope_and_ttl_rejected():
    with patch.dict(os.environ, _ENV):
        with _client() as c:
            headers = {"Tailscale-User-Login": "u@example.com"}
            r = c.post("/api/assistant/tokens", json={"scope": "bogus", "ttlDays": 7},
                      headers=headers)
            assert r.status_code == 422
            r2 = c.post("/api/assistant/tokens", json={"scope": "read", "ttlDays": 99999},
                       headers=headers)
            assert r2.status_code == 422
