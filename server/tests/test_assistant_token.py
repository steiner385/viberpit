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
        # The token is minted for the daemon on THIS host, so a non-loopback peer must
        # be refused. conftest defaults TestClient to a loopback peer (matching the
        # deployed `tailscale serve` -> 127.0.0.1 topology), so this half names its
        # far-away peer explicitly rather than relying on the default.
        far = TestClient(c.app, client=("203.0.113.9", 5000))
        r = far.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
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


def test_bearer_response_sets_no_cookie():
    with patch.dict(os.environ, _ENV):
        tok = mint_token("u@example.com", "sess-1")
        c = TestClient(_client().app, client=("127.0.0.1", 5000))
        with c:
            r = c.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 200
            assert "set-cookie" not in {k.lower() for k in r.headers}
            # Control: a normal Tailscale-identity request DOES set a cookie.
            r2 = c.get("/api/auth/me", headers={"Tailscale-User-Login": "u@example.com"})
            assert "set-cookie" in {k.lower() for k in r2.headers}


# ---------------------------------------------------------------------------
# Cross-process signing key (2026-09-22, sessions 75994c11 / 55f336a6 / d04a1881)
# ---------------------------------------------------------------------------
#
# The daemon MINTS the assistant token and the edge VERIFIES it — two processes with
# two env files. `_signing_secret()` prefers the edge-local session secret over the
# fleet-shared master key, so a daemon that only has the master key (the runbook's
# "must match on every host" secret) and an edge that additionally has a session
# secret sign with DIFFERENT keys: every assistant token fails signature at the edge
# and, in LAN-open mode, the call is silently admitted as the `lan@local` guest.
# Sessions the drawer created were then owned by `lan@local`, so the real user's
# AskUserQuestion answers were rejected as an identity mismatch ("That question is
# no longer pending" over a card that was very much pending).

from types import SimpleNamespace

from server.auth import middleware as _mw

_MASTER = "m" * 32
_COOKIE = "x" * 32


def _signing_settings(monkeypatch, *, master_key, session_secret) -> None:
    """Pin exactly the two signing settings the key selection reads (through
    ``server.auth.middleware.get_settings``) — the daemon has the master key alone,
    the edge has both."""
    ns = SimpleNamespace(master_key=master_key, session_secret=session_secret)
    monkeypatch.setattr(_mw, "get_settings", lambda: ns)


def test_assistant_token_minted_with_master_key_only_verifies_where_session_secret_is_set(monkeypatch):
    _signing_settings(monkeypatch, master_key=_MASTER, session_secret=None)   # daemon
    tok = mint_token("u@example.com", "sess-1")
    _signing_settings(monkeypatch, master_key=_MASTER, session_secret=_COOKIE)  # edge
    assert verify_token(tok, max_age_s=60) == AssistantClaims("u@example.com", "sess-1"), (
        "the daemon (master key only) and the edge (master key + session secret) must "
        "sign the cross-process assistant token with the SAME key"
    )


def test_internal_token_minted_with_master_key_only_verifies_where_session_secret_is_set(monkeypatch):
    from server.auth import internal_token
    _signing_settings(monkeypatch, master_key=_MASTER, session_secret=None)
    tok = internal_token.mint_token()
    _signing_settings(monkeypatch, master_key=_MASTER, session_secret=_COOKIE)
    assert internal_token.verify_token(tok) is True


class _LanOpen:
    """The real settings with LAN-open mode switched on."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name == "allow_unauthenticated":
            return True
        return getattr(self._inner, name)


def test_invalid_assistant_bearer_from_loopback_is_401_not_lan_guest(monkeypatch):
    """A PRESENTED `ccas1:` token that fails verification must be refused outright.

    Falling through to the ordinary identity resolution let the bad token pick up
    the LAN-open guest identity — full guest privileges on every route, and session
    ownership stamped `lan@local` — with nothing in the logs. The control half shows
    the guest path itself still works for a caller that presents no token at all."""
    from server.auth import identity as _identity
    with patch.dict(os.environ, _ENV):
        real = _identity.get_settings
        monkeypatch.setattr(_identity, "get_settings", lambda: _LanOpen(real()))
        c = TestClient(_client().app, client=("127.0.0.1", 5000))
        r = c.get("/api/auth/me", headers={"Authorization": "Bearer ccas1:not-a-real-token"})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "ASSISTANT_TOKEN_INVALID"
        # Control: LAN-open mode is on, so a token-less loopback caller IS a guest.
        r2 = c.get("/api/auth/me")
        assert r2.status_code == 200
        assert r2.json()["identity"] == "lan@local"
