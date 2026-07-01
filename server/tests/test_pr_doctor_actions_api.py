"""/api/automations/pr-doctor propose surface: actionable list + one-tap fix.

Drives the two new endpoints through the real router with a fake orchestrator
installed on ``app.state.sessions`` (a recorder), asserting auth, kind
validation, executor routing for an actionable PR, and the terminal no-op.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import patch

H = {"Tailscale-User-Login": "testuser@example.com"}


class _FakeOrch:
    """Records pr_doctor_fix calls and returns canned actionable lists / statuses."""

    def __init__(self, *, actionable=None, fix_status="sent"):
        self._actionable = actionable or []
        self._fix_status = fix_status
        self.fix_calls: list[tuple[str, int, str]] = []

    async def pr_doctor_actionable(self) -> list[dict]:
        return self._actionable

    async def pr_doctor_fix(self, repo: str, number: int, kind: str) -> str:
        self.fix_calls.append((repo, number, kind))
        return self._fix_status


@contextmanager
def _client(orch: _FakeOrch):
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        real = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        app = create_app(orchestrator=real)
        # Swap in the recorder for the propose-surface calls.
        app.state.sessions = orch
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


# --- POST /pr-doctor/fix -----------------------------------------------------

def test_fix_requires_auth():
    with _client(_FakeOrch()) as c:
        r = c.post("/api/automations/pr-doctor/fix",
                   json={"repo": "o/r", "number": 7, "kind": "failing_ci"})
        assert r.status_code == 401


def test_fix_bad_kind_422():
    orch = _FakeOrch()
    with _client(orch) as c:
        r = c.post("/api/automations/pr-doctor/fix",
                   json={"repo": "o/r", "number": 7, "kind": "nonsense"}, headers=H)
        assert r.status_code == 422
        assert orch.fix_calls == []  # never routed to the executor


def test_fix_routes_to_executor_for_actionable_pr():
    orch = _FakeOrch(fix_status="sent")
    with _client(orch) as c:
        r = c.post("/api/automations/pr-doctor/fix",
                   json={"repo": "o/r", "number": 7, "kind": "failing_ci"}, headers=H)
        assert r.status_code == 200
        assert r.json() == {"status": "sent"}
        assert orch.fix_calls == [("o/r", 7, "failing_ci")]


def test_fix_terminal_pr_is_not_actionable_noop():
    # The executor itself returns "not_actionable" for a terminal PR; the endpoint
    # surfaces that status verbatim (no side effect beyond the executor call).
    orch = _FakeOrch(fix_status="not_actionable")
    with _client(orch) as c:
        r = c.post("/api/automations/pr-doctor/fix",
                   json={"repo": "o/r", "number": 7, "kind": "merge_conflict"}, headers=H)
        assert r.status_code == 200
        assert r.json() == {"status": "not_actionable"}


def test_fix_all_valid_kinds_accepted():
    orch = _FakeOrch()
    with _client(orch) as c:
        for kind in ("out_of_date", "failing_ci", "merge_conflict"):
            r = c.post("/api/automations/pr-doctor/fix",
                       json={"repo": "o/r", "number": 1, "kind": kind}, headers=H)
            assert r.status_code == 200


# --- GET /pr-doctor/actionable ----------------------------------------------

def test_actionable_requires_auth():
    with _client(_FakeOrch()) as c:
        assert c.get("/api/automations/pr-doctor/actionable").status_code == 401


def test_actionable_returns_classified_prs():
    items = [
        {"repo": "o/r", "number": 7, "kind": "failing_ci",
         "url": "https://x/pull/7", "title": "fix ci"},
        {"repo": "o/r", "number": 9, "kind": "out_of_date",
         "url": "https://x/pull/9", "title": "behind"},
    ]
    with _client(_FakeOrch(actionable=items)) as c:
        r = c.get("/api/automations/pr-doctor/actionable", headers=H)
        assert r.status_code == 200
        body = r.json()
        assert body == items
        assert {p["kind"] for p in body} == {"failing_ci", "out_of_date"}
