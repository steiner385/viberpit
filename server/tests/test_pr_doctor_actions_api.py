"""/api/automations/pr-doctor propose surface: actionable list + one-tap fix.

Drives the two new endpoints through the real router with a fake orchestrator
installed on ``app.state.sessions`` (a recorder), asserting auth, kind
validation, executor routing for an actionable PR, and the terminal no-op.

Rewritten onto the generic /{automation_id}/actionable|trigger routes (Task 6).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import patch

H = {"Tailscale-User-Login": "testuser@example.com"}


class _FakeOrch:
    """Records automation_trigger calls; models the trigger status contract
    (invalid kind → structured 'invalid_kind' status, as the real orchestrator does)."""

    def __init__(self, *, actionable=None,
                 result=None, kinds=frozenset({"failing_ci", "merge_conflict", "out_of_date"}),
                 jobs=None):
        self._actionable = actionable or []
        self._result = result or {"status": "sent", "level": "ok", "message": "Sent fix prompt to live session"}
        self._kinds = kinds
        self._jobs = jobs or {}
        self.trigger_calls = []

    async def automation_actionable(self, automation_id):
        return self._actionable

    async def automation_trigger(self, automation_id, item_id, kind):
        if kind not in self._kinds:
            return {"status": "invalid_kind", "level": "error", "message": f"invalid kind {kind!r}"}
        self.trigger_calls.append((item_id, kind))
        return self._result

    async def pr_doctor_job(self, job_id):
        return self._jobs.get(job_id)


@contextmanager
def _client(orch):
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        real = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        app = create_app(orchestrator=real)
        app.state.sessions = orch
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


def test_fix_requires_auth():
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1"}):
        from fastapi.testclient import TestClient
        from server.app import create_app
        from server.sessions.orchestrator import SessionOrchestrator
        from server.tests.fakes.fake_factory import fake_sdk_factory
        app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post("/api/automations/pr-doctor/trigger",
                       json={"item_id": "o/r#7", "kind": "failing_ci"})
    assert r.status_code == 401


def test_fix_bad_kind_422():
    with _client(_FakeOrch()) as c:
        r = c.post("/api/automations/pr-doctor/trigger",
                   json={"item_id": "o/r#7", "kind": "nope"}, headers=H)
    assert r.status_code == 422


def test_fix_routes_to_trigger_for_actionable_pr():
    orch = _FakeOrch()
    with _client(orch) as c:
        r = c.post("/api/automations/pr-doctor/trigger",
                   json={"item_id": "owner/repo#43", "kind": "failing_ci"}, headers=H)
    assert r.status_code == 200
    assert orch.trigger_calls == [("owner/repo#43", "failing_ci")]


def test_job_status_returns_job(): # issue #668
    job = {"job_id": "prd-abc", "target": "o/r#7", "kind": "failing_ci",
           "status": "running", "phase": "cloning", "session_id": None,
           "error_phase": None, "message": "", "level": "ok"}
    with _client(_FakeOrch(jobs={"prd-abc": job})) as c:
        r = c.get("/api/automations/pr-doctor/jobs/prd-abc", headers=H)
    assert r.status_code == 200
    assert r.json() == job


def test_job_status_unknown_is_404():
    with _client(_FakeOrch()) as c:
        r = c.get("/api/automations/pr-doctor/jobs/prd-missing", headers=H)
    assert r.status_code == 404


def test_actionable_returns_classified_prs():
    items = [{"id": "owner/repo#43", "title": "red CI", "subtitle": None,
              "url": "https://x/43",
              "actions": [{"kind": "failing_ci", "label": "Fix CI", "style": "default"}]}]
    with _client(_FakeOrch(actionable=items)) as c:
        r = c.get("/api/automations/pr-doctor/actionable", headers=H)
    assert r.status_code == 200
    assert r.json() == items
