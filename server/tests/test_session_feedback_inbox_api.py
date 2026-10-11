"""API surface of the clustered inbox (#2936): routes → orch.automation_inbox, the
status → HTTP mapping, and the attach-to-session leg the edge runs itself. A stub
orchestrator on app.state.sessions, mirroring test_pr_consolidation_api.py; the
real orchestrator seam is pinned at the bottom."""
from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

H = {"Tailscale-User-Login": "testuser@example.com"}
BASE = "/api/automations/session-feedback/clusters"


class _StubOrch:
    def __init__(self):
        self.calls: list[tuple] = []
        self.sent: list[tuple] = []
        self.sessions = {"live": SimpleNamespace(id="live", status="idle"),
                         "gone": SimpleNamespace(id="gone", status="ended")}
        self.delivery = "sent"

    async def automation_inbox(self, automation_id, op, params=None, identity=None):
        self.calls.append((automation_id, op, params, identity))
        if automation_id != "session-feedback":
            return {"status": "no_provider", "level": "error", "message": "no inbox"}
        if params and params.get("ids") == ["bad"]:
            return {"status": "invalid_ids", "level": "error", "message": "not in the inbox"}
        if op == "clusters":
            return {"clusters": [], "llm": "applied", "total": 0}
        if op == "evidence":
            return {"status": "ok", "level": "ok", "message": "", "text": "EVIDENCE", "count": 1}
        return {"status": op, "level": "ok", "message": "done"}

    async def get_session(self, session_id):
        return self.sessions.get(session_id)

    async def ensure_driver(self, session_id, identity=None):
        return object()

    async def send_prompt(self, session_id, text, interrupt=False):
        self.sent.append((session_id, text))
        return self.delivery


@contextmanager
def _client():
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url,
                                     "VIBERPIT_DEV": "1", "VIBERPIT_TAILNET_ALLOWLIST": ""}):
            real = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=real)
            stub = _StubOrch()
            app.state.sessions = stub
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c, stub


def test_routes_reach_the_inbox_seam_with_their_params():
    with _client() as (c, stub):
        assert c.get(BASE, headers=H).json()["llm"] == "applied"
        assert c.post(f"{BASE}/draft", json={"ids": ["a"]}, headers=H).status_code == 200
        assert c.post(f"{BASE}/file", json={"ids": ["a"], "title": "T", "body": "B"},
                      headers=H).json()["status"] == "file"
        assert c.post(f"{BASE}/dismiss", json={"ids": ["a"], "fixedBy": 12},
                      headers=H).status_code == 200
        assert c.post(f"{BASE}/attach", json={"ids": ["a"], "issue": 9},
                      headers=H).json()["status"] == "attach_issue"
    ops = [(op, params) for _, op, params, _ in stub.calls]
    assert ops == [("clusters", {}), ("draft", {"ids": ["a"]}),
                   ("file", {"ids": ["a"], "title": "T", "body": "B"}),
                   ("dismiss", {"ids": ["a"], "fixedBy": 12}),
                   ("attach_issue", {"ids": ["a"], "issue": 9})]
    assert all(identity for *_, identity in stub.calls)       # the actor rides along


def test_status_mapping_and_body_validation():
    with _client() as (c, _):
        assert c.get("/api/automations/no-such-automation/clusters", headers=H).status_code == 404
        assert c.get("/api/automations/pr-doctor/clusters", headers=H).status_code == 404
        assert c.post(f"{BASE}/dismiss", json={"ids": ["bad"]}, headers=H).status_code == 422
        assert c.post(f"{BASE}/file", json={"ids": ["a"]}, headers=H).status_code == 422
        # attach needs exactly one target
        assert c.post(f"{BASE}/attach", json={"ids": ["a"]}, headers=H).status_code == 422
        assert c.post(f"{BASE}/attach", json={"ids": ["a"], "issue": 1, "sessionId": "live"},
                      headers=H).status_code == 422


def test_attach_session_sends_the_evidence_then_marks_the_rows():
    with _client() as (c, stub):
        r = c.post(f"{BASE}/attach", json={"ids": ["a"], "sessionId": "live"}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "mark_attached_session"
    assert stub.sent == [("live", "EVIDENCE")]
    assert [op for _, op, *_ in stub.calls] == ["evidence", "mark_attached_session"]
    assert stub.calls[-1][2] == {"ids": ["a"], "sessionId": "live"}


def test_attach_session_refuses_unknown_or_ended_and_keeps_rows_on_a_refused_send():
    with _client() as (c, stub):
        assert c.post(f"{BASE}/attach", json={"ids": ["a"], "sessionId": "nope"},
                      headers=H).status_code == 409
        assert c.post(f"{BASE}/attach", json={"ids": ["a"], "sessionId": "gone"},
                      headers=H).status_code == 409
        assert stub.sent == [] and stub.calls == []
        stub.delivery = "draining"
        r = c.post(f"{BASE}/attach", json={"ids": ["a"], "sessionId": "live"}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert [op for _, op, *_ in stub.calls] == ["evidence"]    # never marked


# -- the orchestrator seam ------------------------------------------------------

def test_orchestrator_automation_inbox_dispatches_and_drops_the_count_cache():
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    class _Provider:
        valid_inbox_ops = frozenset({"clusters", "dismiss"})
        inbox_write_ops = frozenset({"dismiss"})

        def __init__(self):
            self.seen = []

        async def inbox(self, op, params, *, identity=None):
            self.seen.append((op, params, identity))
            return {"status": "ok", "level": "ok", "message": ""}

    async def go():
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        p = _Provider()
        orch._action_providers = {"x": p, "plain": object()}
        orch._automation_actionable_expires = 99.0
        assert (await orch.automation_inbox("x", "clusters", None, "me"))["status"] == "ok"
        assert orch._automation_actionable_expires == 99.0          # a read keeps the cache
        await orch.automation_inbox("x", "dismiss", {"ids": ["a"]}, "me")
        assert orch._automation_actionable_expires == 0.0           # a write drops it
        assert p.seen == [("clusters", {}, "me"), ("dismiss", {"ids": ["a"]}, "me")]
        assert (await orch.automation_inbox("x", "nope"))["status"] == "invalid_op"
        assert (await orch.automation_inbox("plain", "clusters"))["status"] == "no_provider"
        assert (await orch.automation_inbox("missing", "clusters"))["status"] == "no_provider"
        # The real Session Feedback provider is registered with an inbox.
        real = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        assert "clusters" in real._action_providers["session-feedback"].valid_inbox_ops
    asyncio.run(go())
