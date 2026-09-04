from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.assistant.token import AssistantClaims, mint_token, verify_token

_ENV = {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:", "CCSWITCH_DEV": "1",
        "CCSWITCH_TAILNET_ALLOWLIST": "", "CCSWITCH_SESSION_SECRET": "x" * 32}


def test_mint_verify_roundtrip():
    with patch.dict(os.environ, _ENV):
        tok = mint_token("u@example.com", "sess-1")
        assert tok.startswith("ccas1:")
        assert verify_token(tok, max_age_s=60) == AssistantClaims("u@example.com", "sess-1")


def test_verify_rejects_tamper_and_expiry():
    with patch.dict(os.environ, _ENV):
        tok = mint_token("u@example.com", "sess-1")
        assert verify_token(tok[:-2] + "zz", max_age_s=60) is None
        assert verify_token("nope", max_age_s=60) is None
        assert verify_token(tok, max_age_s=-1) is None


def _client():
    with patch.dict(os.environ, _ENV):
        app = create_app()
    return TestClient(app)


def test_bearer_from_loopback_sets_identity_and_via():
    with patch.dict(os.environ, _ENV):
        tok = mint_token("u@example.com", "sess-1")
        c = _client()
        r = c.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
        # TestClient's client host is "testclient"; simulate loopback via the scope override:
        assert r.status_code == 401  # non-loopback peer must be refused
        c2 = TestClient(c.app, client=("127.0.0.1", 5000))
        r2 = c2.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
        assert r2.status_code == 200
        assert r2.json()["identity"] == "u@example.com"


def test_bearer_cannot_reach_disallowed_route():
    with patch.dict(os.environ, _ENV):
        tok = mint_token("u@example.com", "sess-1")
        c = TestClient(_client().app, client=("127.0.0.1", 5000))
        r = c.get("/api/settings", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "ASSISTANT_ROUTE_FORBIDDEN"
