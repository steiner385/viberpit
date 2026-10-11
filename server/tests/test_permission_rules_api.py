"""Edge tests for standing always-allow rules: human-only guard, grant-from-card, rules API.

Spec 2026-10-11-always-allow-permission-rules §3.3, §3.4, §4, §5. Harness mirrors
test_sessions_actions_api.py: a real orchestrator only for the lifespan (init_db), a local fake
sessions facade on ``app.state.sessions``, rule/audit rows verified through ``server.db``.
"""
from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {
    "VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:",
    "VIBERPIT_DEV": "1",
    "VIBERPIT_TAILNET_ALLOWLIST": "",
    "VIBERPIT_SESSION_SECRET": "x" * 32,
}
S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"
REPO = "owner/repo"


def _card(perm_id, command, *, engine_managed=False, always_allowed=True, suggestions=None,
          owner="testuser@example.com"):
    return {
        "permId": perm_id, "tool": "Bash", "input": {"command": command}, "turn_id": "t",
        "items": None, "parent_tool_use_id": None, "always_allowed": always_allowed,
        "suggestions": ([] if engine_managed else (
            suggestions if suggestions is not None else
            [{"pattern": f"Bash({command})", "description": "d", "level": "ok"}])),
        "engine_managed": engine_managed, "owner": owner,
    }


def _rec(sid, repo=REPO):
    return SessionRecord(id=sid, name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                         engine_session_id="c", cols=80, rows=24, cwd="/x", detached=False,
                         repo=repo)


class _Fake:
    """Multi-session sessions facade: per-session pending cards, resolving removes the card."""

    def __init__(self):
        self.records = {S1: _rec(S1), S2: _rec(S2)}
        self.pending: dict[str, list[dict]] = {S1: [], S2: []}
        self.resolve_calls: list[tuple] = []
        self.resolve_result = True
        self.raise_for: set[str] = set()
        self.interactions: list[str] = []

    async def get_session(self, sid):
        return self.records.get(sid)

    async def pending_permissions(self, sid):
        if sid in self.raise_for:
            raise RuntimeError("daemon unavailable")
        return list(self.pending.get(sid, []))

    async def resolve_permission(self, sid, perm_id, decision, identity):
        self.resolve_calls.append((sid, perm_id, decision, identity))
        if not self.resolve_result:
            return False
        self.pending[sid] = [p for p in self.pending.get(sid, []) if p["permId"] != perm_id]
        return True

    async def note_user_interaction(self, sid):
        self.interactions.append(sid)


async def _seed_sessions():
    from server.db import db_session
    from server.db.models import Session
    async with db_session() as db:
        for sid in (S1, S2):
            db.add(Session(id=sid, name="n", account_name="a", cwd="/x", repo=REPO,
                           status="waiting_input"))
        await db.commit()


def _make_client(extra_env=None):
    env = {**_ENV, **(extra_env or {})}
    with patch.dict(os.environ, env):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    fake = _Fake()
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


async def _rules():
    from server.permission_rules import store
    from server.db import db_session
    async with db_session() as db:
        return [r.to_wire() for r in await store.list_rules(db)]


async def _audit_types():
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import AuditEvent
    async with db_session() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
        return [(r.type, r.detail) for r in rows]


def _always(client, sid, pid, pattern, scope, confirm=False, headers=_H):
    return client.post(f"/api/sessions/{sid}/permissions/{pid}/always",
                       json={"pattern": pattern, "scope": scope, "confirm": confirm},
                       headers=headers)


def test_grant_from_card_creates_rule_resolves_card_and_sweeps_matching_pending():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test -w web"), _card("p2", "rm x")]
    fake.pending[S2] = [_card("p3", "npm test")]
    with patch.dict(os.environ, _ENV), client:
        client.portal.call(_seed_sessions)
        r = _always(client, S1, "p1", "Bash(npm test:*)", "repo")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rule"]["pattern"] == "Bash(npm test:*)"
        assert body["resolvedPermIds"] == ["p1", "p3"]
        assert {(c[0], c[1], c[2]) for c in fake.resolve_calls} == {
            (S1, "p1", "allow"), (S2, "p3", "allow")}
        assert [p["permId"] for p in fake.pending[S1]] == ["p2"]
        listed = client.get("/api/permission-rules", headers=_H).json()
        assert [x["createdBy"] for x in listed] == ["testuser@example.com"]
        audit = client.portal.call(_audit_types)
    types = [t for t, _ in audit]
    assert "permission_rule.create" in types
    grants = [d for t, d in audit if t == "permission.rule_allow"]
    assert {d["permId"] for d in grants} == {"p1", "p3"}
    assert all(d["trigger"] == "grant" for d in grants)


def test_sweep_includes_engine_managed_cards_and_survives_a_failing_session():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test")]
    fake.pending[S2] = [_card("p9", "npm test", engine_managed=True)]
    with patch.dict(os.environ, _ENV), client:
        client.portal.call(_seed_sessions)
        r = _always(client, S1, "p1", "Bash(npm test:*)", "repo")
        assert r.json()["resolvedPermIds"] == ["p1", "p9"]
        assert (S2, "p9", "allow", "testuser@example.com") in fake.resolve_calls
    client2, fake2 = _make_client()
    fake2.pending[S1] = [_card("p1", "npm test")]
    fake2.raise_for = {S2}
    with patch.dict(os.environ, _ENV), client2:
        client2.portal.call(_seed_sessions)
        r = _always(client2, S1, "p1", "Bash(npm test:*)", "repo")
        assert r.status_code == 200
        assert r.json()["resolvedPermIds"] == ["p1"]


def test_grant_pattern_must_cover_request():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test")]
    with patch.dict(os.environ, _ENV), client:
        r = _always(client, S1, "p1", "Bash(yarn test:*)", "session")
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "PATTERN_DOES_NOT_COVER"
        assert client.portal.call(_rules) == []
        assert fake.resolve_calls == []


def test_grant_refused_pattern():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "git push")]
    with patch.dict(os.environ, _ENV), client:
        r = _always(client, S1, "p1", "Bash(git *)", "session")
        assert r.status_code == 400
        d = r.json()["detail"]
        assert d["code"] == "RULE_REFUSED" and d["reasons"]
        assert client.portal.call(_rules) == []


def test_grant_warn_needs_confirm():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test")]
    with patch.dict(os.environ, _ENV), client:
        r = _always(client, S1, "p1", "Bash(npm:*)", "session")
        assert r.status_code == 409
        d = r.json()["detail"]
        assert d["code"] == "RULE_NEEDS_CONFIRMATION" and d["level"] == "warn" and d["reasons"]
        assert client.portal.call(_rules) == []
        r2 = _always(client, S1, "p1", "Bash(npm:*)", "session", confirm=True)
        assert r2.status_code == 200


def test_global_scope_needs_confirm():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test")]
    with patch.dict(os.environ, _ENV), client:
        r = _always(client, S1, "p1", "Bash(npm test:*)", "global")
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "RULE_NEEDS_CONFIRMATION"
        assert _always(client, S1, "p1", "Bash(npm test:*)", "global", confirm=True
                       ).status_code == 200


def test_grant_rejects_bad_scope_unknown_card_and_engine_managed_creation():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test"), _card("pe", "npm test", engine_managed=True),
                        _card("pn", "npm test", always_allowed=False)]
    with patch.dict(os.environ, _ENV), client:
        assert _always(client, S1, "p1", "Bash(npm test:*)", "nope").json()[
            "detail"]["code"] == "INVALID_SCOPE"
        r = _always(client, S1, "gone", "Bash(npm test:*)", "session")
        assert r.status_code == 404 and r.json()["detail"]["code"] == "PERMISSION_NOT_PENDING"
        for pid in ("pe", "pn"):
            r = _always(client, S1, pid, "Bash(npm test:*)", "session")
            assert r.status_code == 400 and r.json()["detail"]["code"] == "RULE_NOT_ALLOWED"
        assert client.portal.call(_rules) == []


def test_assistant_token_cannot_grant_or_edit():
    from server.assistant.policy import route_allowed
    from server.assistant.token import mint_token

    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test")]
    with patch.dict(os.environ, _ENV), client:
        tok = mint_token("testuser@example.com", "asst-1")
        bearer = {"Authorization": f"Bearer {tok}"}
        lc = TestClient(client.app, client=("127.0.0.1", 5000), raise_server_exceptions=False)
        r = _always(lc, S1, "p1", "Bash(npm test:*)", "session", headers=bearer)
        # The middleware's route allowlist refuses first; require_human (unit-tested
        # below) is the second layer for any other token-borne via.
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "ASSISTANT_ROUTE_FORBIDDEN"
        assert client.portal.call(_rules) == []
        made = _always(client, S1, "p1", "Bash(npm test:*)", "session").json()["rule"]["id"]
        assert lc.patch(f"/api/permission-rules/{made}", json={"pattern": "Bash(npm run:*)"},
                        headers=bearer).status_code == 403
        assert lc.delete(f"/api/permission-rules/{made}", headers=bearer).status_code == 403
        assert len(client.portal.call(_rules)) == 1
    for method, path in (
        ("POST", f"/api/sessions/{S1}/permissions/p1/always"),
        ("POST", "/api/permission-rules/preview"),
        ("PATCH", "/api/permission-rules/x"),
        ("DELETE", "/api/permission-rules/x"),
    ):
        assert route_allowed(method, path) is False
        assert route_allowed(method, path, operator=True) is False


def test_legacy_always_creates_session_rule():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test")]
    with patch.dict(os.environ, _ENV), client:
        client.portal.call(_seed_sessions)
        r = client.post(f"/api/sessions/{S1}/permissions/p1", json={"decision": "always"},
                        headers=_H)
        assert r.status_code == 200
        rules = client.portal.call(_rules)
    assert [(x["scope"], x["scopeKey"], x["pattern"]) for x in rules] == [
        ("session", S1, "Bash(npm test)")]
    assert [c[2] for c in fake.resolve_calls] == ["allow"]


def test_legacy_always_engine_managed_passes_through():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test", engine_managed=True)]
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{S1}/permissions/p1", json={"decision": "always"},
                        headers=_H)
        assert r.status_code == 200
        assert client.portal.call(_rules) == []
    assert [c[2] for c in fake.resolve_calls] == ["always"]


def test_legacy_always_without_ok_suggestion_resolves_allow_only():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test", suggestions=[
        {"pattern": "Bash(npm:*)", "description": "d", "level": "warn"}])]
    with patch.dict(os.environ, _ENV), client:
        r = client.post(f"/api/sessions/{S1}/permissions/p1", json={"decision": "always"},
                        headers=_H)
        assert r.status_code == 200
        assert client.portal.call(_rules) == []
    assert [c[2] for c in fake.resolve_calls] == ["allow"]


def test_require_human_rejects_system_identity_and_any_via():
    import pytest
    from server.sessions import session_actions as sa
    for identity, via in (("system", None), ("u@example.com", "internal"),
                          ("u@example.com", "mcp-external"), ("u@example.com", "assistant")):
        with pytest.raises(sa.ActionError) as ei:
            sa.require_human(identity, via)
        assert ei.value.code == "RULES_HUMAN_ONLY" and ei.value.http == 403
    sa.require_human("u@example.com", None)


def test_preview_reports_validity_coverage_and_also_pending():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test"), _card("p2", "npm test -w web"),
                        _card("p3", "rm x")]
    with patch.dict(os.environ, _ENV), client:
        body = {"sessionId": S1, "permId": "p1", "pattern": "Bash(npm test:*)", "scope": "repo"}
        r = client.post("/api/permission-rules/preview", json=body, headers=_H)
        assert r.status_code == 200
        j = r.json()
        assert j["valid"] is True and j["level"] == "ok" and j["needsConfirmation"] is False
        assert j["coversRequest"] is True and j["alsoPending"] == 1 and j["description"]
        bad = client.post("/api/permission-rules/preview",
                          json={**body, "pattern": "Bash("}, headers=_H).json()
        assert bad["valid"] is False and bad["level"] == "refuse" and bad["description"] is None
        miss = client.post("/api/permission-rules/preview",
                           json={**body, "pattern": "Bash(yarn:*)"}, headers=_H).json()
        assert miss["coversRequest"] is False


def test_patch_and_delete_rule():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test")]
    with patch.dict(os.environ, _ENV), client:
        rid = _always(client, S1, "p1", "Bash(npm test:*)", "session").json()["rule"]["id"]
        # warn pattern needs confirmation, refused pattern never passes
        assert client.patch(f"/api/permission-rules/{rid}", json={"pattern": "Bash(npm:*)"},
                            headers=_H).status_code == 409
        assert client.patch(f"/api/permission-rules/{rid}", json={"pattern": "Bash(git *)"},
                            headers=_H).status_code == 400
        r = client.patch(f"/api/permission-rules/{rid}",
                         json={"pattern": "Bash(npm run:*)"}, headers=_H)
        assert r.status_code == 200 and r.json()["pattern"] == "Bash(npm run:*)"
        assert client.patch("/api/permission-rules/nope", json={"pattern": "Bash(npm run:*)"},
                            headers=_H).status_code == 404
        assert client.delete(f"/api/permission-rules/{rid}", headers=_H).status_code == 204
        assert client.delete(f"/api/permission-rules/{rid}", headers=_H).status_code == 404
        assert client.get("/api/permission-rules", headers=_H).json() == []
        types = [t for t, _ in client.portal.call(_audit_types)]
    assert "permission_rule.update" in types and "permission_rule.revoke" in types


def test_patch_duplicate_is_409():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test"), _card("p2", "npm run build")]
    with patch.dict(os.environ, _ENV), client:
        _always(client, S1, "p1", "Bash(npm test:*)", "session")
        rid = _always(client, S1, "p2", "Bash(npm run:*)", "session").json()["rule"]["id"]
        r = client.patch(f"/api/permission-rules/{rid}", json={"pattern": "Bash(npm test:*)"},
                         headers=_H)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "DUPLICATE_RULE"


def test_list_with_session_id_returns_only_applicable_rules():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test")]
    fake.records[S2] = _rec(S2, repo="other/repo")
    with patch.dict(os.environ, _ENV), client:
        _always(client, S1, "p1", "Bash(npm test:*)", "repo")
        assert len(client.get(f"/api/permission-rules?sessionId={S1}", headers=_H).json()) == 1
        assert client.get(f"/api/permission-rules?sessionId={S2}", headers=_H).json() == []


def test_kill_switch_off_404s_every_route_and_degrades_legacy_always():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test")]
    env = {**_ENV, "VIBERPIT_PERMISSION_RULES_ENABLED": "false"}
    with patch.dict(os.environ, env), client:
        r = _always(client, S1, "p1", "Bash(npm test:*)", "session")
        assert r.status_code == 404 and r.json()["detail"]["code"] == "RULES_DISABLED"
        lc = TestClient(client.app, client=("127.0.0.1", 5000), raise_server_exceptions=False)
        assert lc.delete("/api/permission-rules/x", headers=_H).status_code == 404
        assert client.get("/api/permission-rules", headers=_H).status_code == 404
        assert client.post("/api/permission-rules/preview", headers=_H, json={
            "sessionId": S1, "pattern": "Bash(npm test:*)", "scope": "session"}
        ).status_code == 404
        assert client.patch("/api/permission-rules/x", json={"pattern": "Bash(a)"},
                            headers=_H).status_code == 404
        assert client.delete("/api/permission-rules/x", headers=_H).status_code == 404
        r = client.post(f"/api/sessions/{S1}/permissions/p1", json={"decision": "always"},
                        headers=_H)
        assert r.status_code == 200
    assert [c[2] for c in fake.resolve_calls] == ["allow"]


def test_grant_refuses_up_front_when_card_is_owned_by_another_identity():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test", owner="someone-else@example.com")]
    with patch.dict(os.environ, _ENV), client:
        r = _always(client, S1, "p1", "Bash(npm test:*)", "session")
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "PERMISSION_NOT_OWNED"
        assert client.portal.call(_rules) == []
        assert fake.resolve_calls == []


def test_grant_when_origin_card_vanishes_in_race_keeps_rule_and_returns_200():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test"), _card("p2", "npm test -w web")]
    fake.resolve_result = False
    with patch.dict(os.environ, _ENV), client:
        client.portal.call(_seed_sessions)
        r = _always(client, S1, "p1", "Bash(npm test:*)", "session")
        assert r.status_code == 200
        assert r.json()["resolvedPermIds"] == []
        assert len(client.portal.call(_rules)) == 1


def test_preview_also_pending_and_sweep_skip_cards_that_cannot_be_always_allowed():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test"), _card("p2", "npm test -w web"),
                        _card("p3", "npm test -w api", always_allowed=False)]
    with patch.dict(os.environ, _ENV), client:
        client.portal.call(_seed_sessions)
        body = {"sessionId": S1, "permId": "p1", "pattern": "Bash(npm test:*)", "scope": "session"}
        assert client.post("/api/permission-rules/preview", json=body,
                           headers=_H).json()["alsoPending"] == 1
        r = _always(client, S1, "p1", "Bash(npm test:*)", "session")
        assert r.json()["resolvedPermIds"] == ["p1", "p2"]


def test_sweep_survives_daemon_unavailable_at_info_level(caplog):
    import logging

    from server.ipc.client import DaemonUnavailable

    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test")]
    orig = fake.pending_permissions

    async def flaky(sid):
        if sid == S2:
            raise DaemonUnavailable("down")
        return await orig(sid)

    fake.pending_permissions = flaky
    with patch.dict(os.environ, _ENV), client, caplog.at_level(logging.INFO):
        client.portal.call(_seed_sessions)
        assert _always(client, S1, "p1", "Bash(npm test:*)", "repo").status_code == 200
    recs = [r for r in caplog.records if "sweep of session" in r.getMessage()]
    assert recs and all(r.levelno == logging.INFO for r in recs)
