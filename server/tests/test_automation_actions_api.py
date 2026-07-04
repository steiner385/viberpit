import os
from contextlib import contextmanager
from unittest.mock import patch

H = {"Tailscale-User-Login": "testuser@example.com"}


class _FakeOrch:
    def __init__(self, *, actionable=None, result=None):
        self._actionable = actionable or []
        self._result = result or {"status": "spawned", "level": "ok", "message": "Spawned fix session"}
        self.trigger_calls = []

    async def automation_actionable(self, automation_id):
        return self._actionable

    async def automation_trigger(self, automation_id, item_id, kind):
        self.trigger_calls.append((automation_id, item_id, kind))
        return self._result


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


def test_actionable_lists_items():
    # Use "autopilot" — a registered automation with no literal /autopilot/actionable route,
    # so the generic /{automation_id}/actionable route is the unambiguous match.
    items = [{"id": "o/r#7", "title": "red", "subtitle": None, "url": "https://x/7",
              "actions": [{"kind": "failing_ci", "label": "Fix CI", "style": "default"}]}]
    with _client(_FakeOrch(actionable=items)) as c:
        r = c.get("/api/automations/autopilot/actionable", headers=H)
    assert r.status_code == 200
    assert r.json() == items


def test_actionable_provider_less_returns_empty():
    # cicd-doctor is a real registry automation with no action provider → 200 [].
    with _client(_FakeOrch(actionable=[])) as c:
        r = c.get("/api/automations/cicd-doctor/actionable", headers=H)
    assert r.status_code == 200
    assert r.json() == []


def test_actionable_unknown_automation_404():
    with _client(_FakeOrch()) as c:
        r = c.get("/api/automations/does-not-exist/actionable", headers=H)
    assert r.status_code == 404


def test_trigger_ok_returns_result():
    orch = _FakeOrch()
    with _client(orch) as c:
        r = c.post("/api/automations/autopilot/trigger",
                   json={"item_id": "o/r#7", "kind": "failing_ci"}, headers=H)
    assert r.status_code == 200
    assert r.json()["level"] == "ok"
    assert orch.trigger_calls == [("autopilot", "o/r#7", "failing_ci")]


def test_trigger_invalid_kind_maps_422():
    bad = {"status": "invalid_kind", "level": "error", "message": "invalid kind 'bogus'"}
    with _client(_FakeOrch(result=bad)) as c:
        r = c.post("/api/automations/autopilot/trigger",
                   json={"item_id": "o/r#7", "kind": "bogus"}, headers=H)
    assert r.status_code == 422


def test_trigger_no_provider_maps_404():
    bad = {"status": "no_provider", "level": "error", "message": "automation has no action provider"}
    with _client(_FakeOrch(result=bad)) as c:
        r = c.post("/api/automations/cicd-doctor/trigger",
                   json={"item_id": "o/r#7", "kind": "failing_ci"}, headers=H)
    assert r.status_code == 404
