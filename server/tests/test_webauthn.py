"""Passkeys (#1410, FR-G12.6–.11): the app-level strong-authentication path, end to end.

A software authenticator (``_soft_authenticator.py``) drives the server's real
py_webauthn verification; nothing on the server side is stubbed.
"""
from __future__ import annotations

import os
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.tests._soft_authenticator import SoftAuthenticator

ORIGIN = "https://testserver"
OWNER = "owner@example.com"
_TAILNET = {"Tailscale-User-Login": OWNER}
_ENV = {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:", "VIBERPIT_DEV": "1",
        "VIBERPIT_TAILNET_ALLOWLIST": "", "VIBERPIT_SESSION_SECRET": "s" * 32,
        "VIBERPIT_WEBAUTHN_ENABLED": "true", "VIBERPIT_PUBLIC_ORIGIN": ORIGIN}


@pytest.fixture
def client():
    from server.api.ratelimit import passkey_login_limiter
    from server.auth import webauthn
    webauthn.reset_ceremonies()
    passkey_login_limiter._buckets.clear()
    env = {k: v for k, v in os.environ.items() if k != "VIBERPIT_DEV_INSECURE_COOKIE"}
    env.update(_ENV)
    with patch.dict(os.environ, env, clear=True):
        with TestClient(create_app(), base_url=ORIGIN) as c:
            yield c


@pytest.fixture
def authn():
    return SoftAuthenticator("testserver", ORIGIN)


def _enrol(c: TestClient, authn: SoftAuthenticator, label: str = "laptop") -> dict:
    begin = c.post("/api/auth/webauthn/register/begin", headers=_TAILNET, json={})
    assert begin.status_code == 200, begin.text
    done = c.post("/api/auth/webauthn/register/complete", headers=_TAILNET, json={
        "ceremonyId": begin.json()["ceremonyId"], "label": label,
        "credential": authn.create(begin.json()["publicKey"])})
    assert done.status_code == 201, done.text
    c.cookies.clear()
    return done.json()


def _login(c: TestClient, authn: SoftAuthenticator, **get_kwargs):
    c.cookies.clear()
    begin = c.post("/api/auth/webauthn/login/begin", json={})
    assert begin.status_code == 200, begin.text
    body = begin.json()
    return c.post("/api/auth/webauthn/login/complete", json={
        "ceremonyId": body["ceremonyId"],
        "credential": authn.get(body["publicKey"], **get_kwargs)}), body


def _db(c: TestClient, fn):
    async def _run():
        from server.db import db_session
        async with db_session() as db:
            out = await fn(db)
            await db.commit()
            return out
    assert c.portal is not None
    return c.portal.call(_run)


async def _audit_types(db):
    from sqlalchemy import select

    from server.db.models import AuditEvent
    return [(r.type, (r.detail or {}).get("reason"))
            for r in (await db.execute(select(AuditEvent))).scalars()]


# --- AC-G12.4: the ceremony end to end ----------------------------------------------


def test_register_then_login_round_trip_sets_secure_httponly_strict_cookie(client, authn):
    cred = _enrol(client, authn)
    assert cred["label"] == "laptop" and cred["transports"] == ["internal"]

    resp, _ = _login(client, authn)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"identity": OWNER, "authMode": "passkey"}
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie

    me = client.get("/api/auth/me")      # cookie only: no tailnet header at all
    assert me.status_code == 200
    assert me.json() == {"identity": OWNER, "scope": "full", "authMode": "passkey"}

    async def _row(db):
        from sqlalchemy import select

        from server.db.models import AppSession
        return (await db.execute(select(AppSession).where(
            AppSession.auth_method == "passkey"))).scalar_one()
    session = _db(client, _row)
    assert session.identity == OWNER and session.credential_id == cred["id"]


def test_me_reports_tailscale_for_ambient_identity(client):
    assert client.get("/api/auth/me", headers=_TAILNET).json()["authMode"] == "tailscale"


def test_usernameless_and_named_login_both_list_correctly(client, authn):
    cred = _enrol(client, authn)
    named = client.post("/api/auth/webauthn/login/begin", json={"identity": OWNER}).json()
    assert [c["id"] for c in named["publicKey"]["allowCredentials"]] == [
        _b64(authn.credential_id)]
    anon = client.post("/api/auth/webauthn/login/begin", json={}).json()
    assert anon["publicKey"].get("allowCredentials", []) == []
    assert cred["id"]


def _b64(raw: bytes) -> str:
    from webauthn.helpers import bytes_to_base64url
    return bytes_to_base64url(raw)


def test_passkey_session_counts_as_a_person_for_human_only_gates(client, authn, monkeypatch):
    """A LAN device no front-end can vouch for is still a person once it has passed a
    user-verified passkey ceremony."""
    from server.auth import identity
    monkeypatch.setattr(identity, "attested_front_end_hop", lambda _conn: False)
    conn = SimpleNamespace(state=SimpleNamespace(via=None, auth_method="passkey"))
    assert identity.human_gate_via(conn) is None
    conn.state.auth_method = "tailscale"
    assert identity.human_gate_via(conn) == identity.LOCAL_PROCESS_VIA


def test_ambient_request_never_adopts_a_passkey_session(client, authn):
    _enrol(client, authn)
    resp, _ = _login(client, authn)
    assert resp.status_code == 200
    client.cookies.clear()
    client.get("/api/auth/me", headers=_TAILNET)

    async def _methods(db):
        from sqlalchemy import select

        from server.db.models import AppSession
        return sorted(str(r.auth_method) for r in (await db.execute(select(AppSession))).scalars())
    assert _db(client, _methods) == ["None", "passkey"]


@pytest.mark.asyncio
async def test_websocket_accepts_a_passkey_session(client, authn):
    from server.auth.middleware import require_ws_identity
    _enrol(client, authn)
    resp, _ = _login(client, authn)
    signed = resp.cookies.get("viberpit_session") or next(iter(resp.cookies.values()))
    ws = SimpleNamespace(cookies={"viberpit_session": signed}, headers={},
                         state=SimpleNamespace())

    async def _resolve():
        return await require_ws_identity(ws)  # type: ignore[arg-type]
    assert client.portal is not None
    assert client.portal.call(_resolve) == OWNER
    assert ws.state.auth_method == "passkey"


# --- AC-G12.5: negative paths -------------------------------------------------------


def test_register_begin_forbidden_for_non_tailnet_or_display(client, authn, monkeypatch):
    # unidentified: 401 before the route runs
    client.cookies.clear()
    assert client.post("/api/auth/webauthn/register/begin", json={}).status_code == 401
    # display scope: the kiosk principal may not enrol
    tok = client.post("/api/assistant/tokens", headers=_TAILNET,
                      json={"scope": "display", "ttlDays": 1, "label": "wall"}).json()
    client.post("/api/kiosk/session", content=f"token={tok['token']}",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False)
    r = client.post("/api/auth/webauthn/register/begin", json={})
    assert r.status_code in (403,), r.text
    client.cookies.clear()
    # an agent process on this host asserting the owner's header: not a person
    from server.auth import identity
    monkeypatch.setattr(identity, "attested_front_end_hop", lambda _conn: False)
    r = client.post("/api/auth/webauthn/register/begin", headers=_TAILNET, json={})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "PASSKEY_ENROL_FORBIDDEN"


def test_may_enrol_rule():
    from server.auth.webauthn import may_enrol
    ok = {"identity": OWNER, "scope": "full", "auth_method": "tailscale", "human": True}
    assert may_enrol(**ok)
    assert may_enrol(**{**ok, "auth_method": "passkey"})
    for bad in ({"auth_method": "bearer"}, {"auth_method": "display"}, {"scope": "display"},
                {"human": False}, {"identity": None}):
        assert not may_enrol(**{**ok, **bad}), bad
    with patch.dict(os.environ, {"VIBERPIT_TAILNET_ALLOWLIST": "someone@else.com"}):
        assert not may_enrol(**ok)


def test_replayed_challenge_400(client, authn):
    _enrol(client, authn)
    resp, body = _login(client, authn)
    assert resp.status_code == 200
    replay = client.post("/api/auth/webauthn/login/complete", json={
        "ceremonyId": body["ceremonyId"], "credential": authn.get(body["publicKey"])})
    assert replay.status_code == 400
    assert replay.json()["error"]["code"] == "PASSKEY_CEREMONY_INVALID"


def test_revoked_credential_401(client, authn):
    cred = _enrol(client, authn)
    assert client.delete(f"/api/auth/credentials/{cred['id']}",
                         headers=_TAILNET).status_code == 204
    resp, _ = _login(client, authn)
    assert resp.status_code == 401


def test_foreign_origin_assertion_401(client, authn):
    _enrol(client, authn)
    resp, _ = _login(client, authn, origin="https://evil.example")
    assert resp.status_code == 401
    assert ("auth.passkey.fail", "assertion_invalid") in _db(client, _audit_types)


def test_sign_count_regression_401_and_audit(client, authn):
    _enrol(client, authn)
    assert _login(client, authn, sign_count=5)[0].status_code == 200
    resp, _ = _login(client, authn, sign_count=3)     # a cloned authenticator
    assert resp.status_code == 401
    assert ("auth.passkey.fail", "sign_count_regression") in _db(client, _audit_types)


def test_tampered_cookie_401(client, authn):
    _enrol(client, authn)
    resp, _ = _login(client, authn)
    signed = resp.cookies.get("viberpit_session")
    client.cookies.clear()
    client.cookies.set("viberpit_session", signed[:-2] + ("A" if signed[-2] != "A" else "B")
                       + signed[-1])
    assert client.get("/api/auth/me").status_code == 401


# --- AC-G12.6: rate limit -----------------------------------------------------------


def test_login_rate_limit_429_retry_after_uses_shared_limiter(client):
    from server.api import auth as auth_api
    from server.api import ratelimit
    assert auth_api.passkey_login_limiter is ratelimit.passkey_login_limiter
    codes = []
    for _ in range(int(ratelimit.PASSKEY_LOGIN_CAPACITY) + 1):
        r = client.post("/api/auth/webauthn/login/complete",
                        json={"ceremonyId": "nope", "credential": {}})
        codes.append(r.status_code)
    assert codes[-1] == 429 and int(r.headers["Retry-After"]) >= 1
    assert set(codes[:-1]) == {400}
    reasons = [reason for t, reason in _db(client, _audit_types) if t == "auth.passkey.fail"]
    assert len(reasons) == len(codes)          # one audit event per failure
    assert reasons[-1] == "rate_limited"


# --- AC-G12.7: idle and absolute expiry ---------------------------------------------


def _age_passkey_session(c: TestClient, **fields):
    async def _age(db):
        from sqlalchemy import update

        from server.db.models import AppSession
        await db.execute(update(AppSession).where(AppSession.auth_method == "passkey")
                         .values(**fields))
    _db(c, _age)


def _last_seen(c: TestClient):
    async def _read(db):
        from sqlalchemy import select

        from server.db.models import AppSession
        return (await db.execute(select(AppSession.last_seen_at).where(
            AppSession.auth_method == "passkey"))).scalar_one()
    return _db(c, _read)


def test_idle_timeout_expires_and_touch_advances_last_seen(client, authn):
    from server.config import get_settings
    from server.db.clock import naive_utc_now
    _enrol(client, authn)
    _login(client, authn)
    idle = get_settings().session_idle_s
    _age_passkey_session(client, last_seen_at=naive_utc_now() - timedelta(seconds=idle - 60))
    before = _last_seen(client)
    assert client.get("/api/auth/me").status_code == 200
    assert _last_seen(client) > before
    _age_passkey_session(client, last_seen_at=naive_utc_now() - timedelta(seconds=idle + 1))
    assert client.get("/api/auth/me").status_code == 401


def test_absolute_24h_still_enforced(client, authn):
    from server.db.clock import naive_utc_now
    _enrol(client, authn)
    _login(client, authn)
    _age_passkey_session(client, expires_at=naive_utc_now() - timedelta(seconds=1))
    assert client.get("/api/auth/me").status_code == 401


# --- AC-G12.8: revocation and recovery ----------------------------------------------


def test_delete_credential_revokes_its_sessions_and_tailnet_still_works(client, authn):
    cred = _enrol(client, authn)
    resp, _ = _login(client, authn)
    signed = resp.cookies.get("viberpit_session")
    assert client.get("/api/auth/me").status_code == 200
    listed = client.get("/api/auth/credentials").json()
    assert [c["id"] for c in listed] == [cred["id"]]

    assert client.delete(f"/api/auth/credentials/{cred['id']}").status_code == 204
    client.cookies.clear()
    client.cookies.set("viberpit_session", signed)
    assert client.get("/api/auth/me").status_code == 401           # its session is dead
    client.cookies.clear()
    me = client.get("/api/auth/me", headers=_TAILNET)                # recovery path
    assert me.status_code == 200 and me.json()["authMode"] == "tailscale"
    assert client.get("/api/auth/credentials", headers=_TAILNET).json() == []


def test_cannot_revoke_someone_elses_passkey(client, authn):
    cred = _enrol(client, authn)
    other = {"Tailscale-User-Login": "mallory@example.com"}
    assert client.delete(f"/api/auth/credentials/{cred['id']}", headers=other).status_code == 404


# --- AC-G12.10: off means absent ----------------------------------------------------


def test_routes_404_when_disabled_and_no_not_implemented_in_auth(client, monkeypatch):
    monkeypatch.setenv("VIBERPIT_WEBAUTHN_ENABLED", "false")
    for method, path in (("post", "/api/auth/webauthn/register/begin"),
                         ("post", "/api/auth/webauthn/register/complete"),
                         ("post", "/api/auth/webauthn/login/begin"),
                         ("post", "/api/auth/webauthn/login/complete"),
                         ("get", "/api/auth/credentials"),
                         ("delete", "/api/auth/credentials/x")):
        r = getattr(client, method)(path, headers=_TAILNET,
                                    **({"json": {}} if method == "post" else {}))
        assert r.status_code == 404, (path, r.status_code)
    assert client.get("/api/config", headers=_TAILNET).json()["webauthnEnabled"] is False
    auth_dir = Path(__file__).resolve().parents[1] / "auth"
    hits = subprocess.run(["grep", "-rn", "NotImplementedError", str(auth_dir)],
                          capture_output=True, text=True).stdout
    assert hits == ""


def test_disabling_passkeys_ends_passkey_sessions(client, authn, monkeypatch):
    _enrol(client, authn)
    _login(client, authn)
    assert client.get("/api/auth/me").status_code == 200
    monkeypatch.setenv("VIBERPIT_WEBAUTHN_ENABLED", "false")
    assert client.get("/api/auth/me").status_code == 401


def test_rp_id_and_origins_come_from_the_origin_allowlist(client):
    from server.auth import origin, webauthn
    assert webauthn.rp_id() == "testserver"
    assert webauthn.expected_origins() == sorted(origin.configured_origins())


# --- FR-G12.12: a device with no identity can load the shell and reach /login --------


def test_spa_shell_loads_without_identity_only_when_passkeys_are_on(tmp_path, monkeypatch):
    from server.spa import mount_spa
    (tmp_path / "index.html").write_text("<!doctype html><title>shell</title>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    env = {k: v for k, v in os.environ.items() if k != "VIBERPIT_DEV_INSECURE_COOKIE"}
    env.update(_ENV)
    with patch.dict(os.environ, env, clear=True):
        app = create_app()
        mount_spa(app, tmp_path)
        with TestClient(app, base_url=ORIGIN) as c:
            for path in ("/login", "/assets/app.js", "/sessions/abc"):
                r = c.get(path)
                assert r.status_code == 200, (path, r.status_code)
            assert "<!doctype html>" in c.get("/login").text.lower()
            # nothing else opens up: data routes, the API schema and docs stay gated
            for path in ("/api/auth/me", "/api/sessions", "/openapi.json", "/docs"):
                assert c.get(path).status_code == 401, path
            assert c.post("/login").status_code == 401
            monkeypatch.setenv("VIBERPIT_WEBAUTHN_ENABLED", "false")
            assert c.get("/login").status_code == 401


def test_passkey_session_enrols_only_right_after_sign_in(client, authn):
    """A stolen passkey cookie must not mint a credential that outlives it: enrolment
    from a passkey session needs a sign-in within ENROL_REAUTH_WINDOW (ASVS V3.7)."""
    from server.auth.webauthn import ENROL_REAUTH_WINDOW
    from server.db.clock import naive_utc_now
    _enrol(client, authn)
    _login(client, authn)
    fresh = client.post("/api/auth/webauthn/register/begin", json={})
    assert fresh.status_code == 200, fresh.text
    _age_passkey_session(client, created_at=naive_utc_now() - ENROL_REAUTH_WINDOW
                         - timedelta(seconds=1))
    stale = client.post("/api/auth/webauthn/register/begin", json={})
    assert stale.status_code == 403
    assert stale.json()["detail"]["code"] == "PASSKEY_REAUTH_REQUIRED"
    assert client.get("/api/auth/me").status_code == 200   # the session itself still works
