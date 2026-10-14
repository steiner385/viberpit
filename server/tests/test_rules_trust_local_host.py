"""Opt-in trust of the edge host for human-only writes (spec 2026-10-14)."""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import time
from unittest.mock import patch

import pytest

from server.auth import identity
from server.auth.identity import LOCAL_PROCESS_VIA, LOCAL_TRUSTED_VIA, human_gate_via
from server.config import get_settings
from server.sessions.session_actions import ActionError, require_human
from server.tests.test_local_identity_assertion import (
    _FRONT_ENDS, _OPERATOR_UID, _OWN, _PHONE, _REAL_ATTEST, _UIDS, _XFF, _conn,
    _local_client, _refused,
)
from server.tests.test_permission_rules_api import (
    _ENV, _H, S1, _always, _card, _make_client, _rules, _seed_sessions,
)
from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events, ws_env  # noqa: F401


def test_rules_trust_local_host_defaults_off():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VIBERPIT_RULES_TRUST_LOCAL_HOST", None)
        os.environ.pop("CCSWITCH_RULES_TRUST_LOCAL_HOST", None)
        assert get_settings().rules_trust_local_host is False


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True),
    ("false", False), ("0", False),
])
def test_rules_trust_local_host_parses_like_sibling_flags(raw, expected):
    with patch.dict(os.environ, {"VIBERPIT_RULES_TRUST_LOCAL_HOST": raw,
                                 "VIBERPIT_PERMISSION_RULES_ENABLED": raw}):
        s = get_settings()
        assert s.rules_trust_local_host is expected
        assert s.rules_trust_local_host == s.permission_rules_enabled


def test_rules_trust_local_host_is_listed_on_about():
    from server.api.settings import _MANAGED
    row = next(r for r in _MANAGED if r[0] == "rules_trust_local_host")
    assert row[1] == "VIBERPIT_RULES_TRUST_LOCAL_HOST"
    assert "agents on the host" in row[2]


_ON = {"VIBERPIT_RULES_TRUST_LOCAL_HOST": "true"}


@pytest.fixture
def real_attestation(monkeypatch):
    # conftest stubs the attestation for every suite; these tests need the real one.
    monkeypatch.setattr(identity, "attested_front_end_hop", _REAL_ATTEST)
    monkeypatch.setattr(identity, "_front_end_headers", lambda: dict(_FRONT_ENDS))
    monkeypatch.setattr(identity, "_self_tailscale_ips",
                        lambda: frozenset({ipaddress.ip_address(_OWN)}))


@pytest.mark.parametrize("on,host,uids,headers,expected", [
    (True, "127.0.0.1", {_OPERATOR_UID}, {}, LOCAL_TRUSTED_VIA),       # host browser / curl
    (True, "127.0.0.1", {0}, {_XFF: _OWN}, LOCAL_TRUSTED_VIA),         # host browser via serve
    (True, "127.0.0.1", None, {}, LOCAL_TRUSTED_VIA),                  # /proc unreadable
    (True, "127.0.0.1", {0}, {_XFF: _PHONE}, None),                    # phone via serve: still human
    (True, _PHONE, set(), {}, None),                                   # remote peer
    (False, "127.0.0.1", {_OPERATOR_UID}, {}, LOCAL_PROCESS_VIA),
    (False, "127.0.0.1", {0}, {_XFF: _OWN}, LOCAL_PROCESS_VIA),
])
def test_human_gate_via_with_trust_local_host(real_attestation, on, host, uids, headers,
                                              expected):
    env = _ON if on else {"VIBERPIT_RULES_TRUST_LOCAL_HOST": "false"}
    with patch.dict(os.environ, env), patch(_UIDS, return_value=uids):
        assert human_gate_via(_conn(host, headers=headers)) == expected


def test_trust_local_host_never_admits_token_borne_callers(real_attestation):
    with patch.dict(os.environ, _ON), \
            patch(_UIDS, side_effect=AssertionError("must not read /proc")):
        for via in ("assistant", "mcp", "cli", "internal", "session"):
            assert human_gate_via(_conn("127.0.0.1", via=via)) == via
            with pytest.raises(ActionError) as exc:
                require_human("testuser@example.com", via)
            assert exc.value.code == "RULES_HUMAN_ONLY"


def test_require_human_admits_local_trusted_but_not_the_system_identity():
    from server.sessions.session_actions import SYSTEM_IDENTITY
    require_human("testuser@example.com", LOCAL_TRUSTED_VIA)   # no raise
    with pytest.raises(ActionError):
        require_human(SYSTEM_IDENTITY, LOCAL_TRUSTED_VIA)


def test_same_host_refusal_names_the_opt_in():
    with pytest.raises(ActionError) as exc:
        require_human("testuser@example.com", LOCAL_PROCESS_VIA)
    assert exc.value.http == 403
    assert "VIBERPIT_RULES_TRUST_LOCAL_HOST" in exc.value.message
    assert "agents on this host" in exc.value.message


def test_host_browser_writes_rules_and_profiles_when_trusted(real_attestation):
    client, fake = _make_client(_ON)
    fake.pending[S1] = [_card("p1", "npm test"), _card("p2", "npm run build")]
    with patch.dict(os.environ, {**_ENV, **_ON}), client:
        client.portal.call(_seed_sessions)
        lc = _local_client(client)
        with patch(_UIDS, return_value={_OPERATOR_UID}):
            r = _always(lc, S1, "p1", "Bash(npm test:*)", "session")
            assert r.status_code == 200, r.text
            rid = r.json()["rule"]["id"]
            assert r.json()["rule"]["createdVia"] == LOCAL_TRUSTED_VIA
            assert lc.post(f"/api/sessions/{S1}/permissions/p2",
                           json={"decision": "always"}, headers=_H).status_code == 200
            assert lc.patch(f"/api/permission-rules/{rid}",
                            json={"pattern": "Bash(npm run:*)"}, headers=_H).status_code == 200
            assert lc.post("/api/permission-profiles", headers=_H,
                           json={"name": "q", "patterns": ["Read"]}).status_code in (200, 201)
            assert lc.delete(f"/api/permission-rules/{rid}", headers=_H).status_code == 204
        audits = client.portal.call(_audit_vias)
    assert audits.get("permission_rule.create") == LOCAL_TRUSTED_VIA
    assert audits.get("permission_rule.revoke") == LOCAL_TRUSTED_VIA


def test_host_browser_still_refused_when_not_trusted(real_attestation):
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test")]
    with patch.dict(os.environ, {**_ENV, "VIBERPIT_RULES_TRUST_LOCAL_HOST": "false"}), client:
        client.portal.call(_seed_sessions)
        lc = _local_client(client)
        with patch(_UIDS, return_value={_OPERATOR_UID}):
            _refused(_always(lc, S1, "p1", "Bash(npm test:*)", "session"))
        assert client.portal.call(_rules) == []


async def _audit_vias():
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AuditEvent
    async with db_session() as db:
        rows = (await db.execute(select(AuditEvent.type, AuditEvent.via))).all()
    return {t: v for t, v in rows}


def test_ws_always_from_host_browser_uses_local_trusted(ws_env, real_attestation):  # noqa: F811
    _app, orch, client = ws_env([])
    session_id = client.portal.call(orch.create_session, "test-ws-always-trusted").id
    seen: list[tuple[str, str | None]] = []

    async def _capture(*_a, decision, via, **_k):
        seen.append((decision, via))
        return {}

    with patch.dict(os.environ, _ON), \
            patch("server.bridge.ws.session_actions.resolve_permission", _capture), \
            patch(_UIDS, return_value={_OPERATOR_UID}):
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
            _drain_events(ws, until_type="replayComplete")
            ws.send_text(json.dumps({"type": "permissionResponse", "sessionId": session_id,
                                     "permId": "p-always", "decision": "always"}))
            deadline = time.monotonic() + 5.0
            while not seen and time.monotonic() < deadline:
                time.sleep(0.02)
    client.portal.call(orch.close_session, session_id)
    assert seen == [("always", LOCAL_TRUSTED_VIA)]


def test_startup_logs_trust_local_host_at_info_only_when_on(caplog):
    from server.app import _log_rules_trust_local_host
    with caplog.at_level(logging.INFO, logger="server.auth"):
        with patch.dict(os.environ, {"VIBERPIT_RULES_TRUST_LOCAL_HOST": "false"}):
            _log_rules_trust_local_host()
        assert not caplog.records
        with patch.dict(os.environ, _ON):
            _log_rules_trust_local_host()
    [rec] = caplog.records
    assert rec.levelno == logging.INFO
    assert "VIBERPIT_RULES_TRUST_LOCAL_HOST" in rec.getMessage()
