from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.api.deploy import router, get_deploy_client
from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import AuditEvent
from server.deploy.client import DeployControllerUnavailable


class FakeClient:
    def __init__(self, status_body=None, start_ret=(200, {"started": True}), unavailable=False):
        self._status = status_body or {"pending": {"behind": True}, "deploy": {"running": False}}
        self._start = start_ret
        self._unavailable = unavailable
        self.start_calls: list[str | None] = []

    async def get_status(self):
        if self._unavailable:
            raise DeployControllerUnavailable("x")
        return self._status

    async def start(self, target_sha=None):
        if self._unavailable:
            raise DeployControllerUnavailable("x")
        self.start_calls.append(target_sha)
        return self._start


class FakeDB:
    """Captures what record_audit() adds; get_db normally commits for us."""

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)


def audits(client) -> list[AuditEvent]:
    return [o for o in client.fake_db.added if isinstance(o, AuditEvent)]


class FakeOrch:
    def __init__(self, live):
        self._live = live

    async def resume_recovery_status(self):
        return self._live


def make_app(client, orch=None):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[current_identity] = lambda: "tester"
    app.dependency_overrides[get_deploy_client] = lambda: client
    db = FakeDB()
    app.dependency_overrides[get_db] = lambda: db
    if orch is not None:
        app.state.sessions = orch
    tc = TestClient(app)
    tc.fake_db = db
    return tc


def test_status_proxies_controller():
    c = make_app(FakeClient())
    body = c.get("/api/deploy/status").json()
    assert body["pending"]["behind"] is True


def test_status_unavailable_is_503_not_a_deploy_error():
    """A transient controller-unreachable must surface as 503 (like /start), NOT a 200 whose
    deploy.error='controller unavailable' — the frontend reads any deploy.error as a terminal
    'Upgrade failed', so a mid-deploy blip would flash a spurious failure. 503 keeps react-query
    on last-good data + isError, routing it through the existing 'reconnecting' path instead."""
    c = make_app(FakeClient(unavailable=True))
    r = c.get("/api/deploy/status")
    assert r.status_code == 503
    assert r.json().get("deploy", {}).get("error") != "controller unavailable"


def test_status_reconciles_failed_with_daemon_live_set():
    """The controller's frozen restore.failed is overwritten by the daemon's live
    view, so a recovered session drops off the banner and a gave-up one is flagged."""
    controller_status = {"pending": {"behind": False},
                         "deploy": {"running": False, "restore": {"failed": ["a", "b", "c"]}}}
    orch = FakeOrch({"failed": ["b"], "gaveUp": ["c"]})   # 'a' recovered; 'c' gave up
    c = make_app(FakeClient(status_body=controller_status), orch=orch)
    restore = c.get("/api/deploy/status").json()["deploy"]["restore"]
    assert restore["failed"] == ["b"]
    assert restore["gaveUp"] == ["c"]


def test_status_reconciliation_clears_banner_when_all_recovered():
    controller_status = {"pending": {"behind": False},
                         "deploy": {"running": False, "restore": {"failed": ["a", "b"]}}}
    c = make_app(FakeClient(status_body=controller_status),
                 orch=FakeOrch({"failed": [], "gaveUp": []}))
    restore = c.get("/api/deploy/status").json()["deploy"]["restore"]
    assert restore["failed"] == [] and restore["gaveUp"] == []


def test_status_without_daemon_leaves_controller_list_untouched():
    controller_status = {"pending": {"behind": False},
                         "deploy": {"running": False, "restore": {"failed": ["a"]}}}
    c = make_app(FakeClient(status_body=controller_status))   # no orch on app.state
    restore = c.get("/api/deploy/status").json()["deploy"]["restore"]
    assert restore["failed"] == ["a"]


def test_start_proxies_status_code():
    c = make_app(FakeClient(start_ret=(409, {"started": False, "reason": "in progress"})))
    r = c.post("/api/deploy/start")
    assert r.status_code == 409 and r.json()["started"] is False


def test_start_forwards_target_sha_to_controller():
    fc = FakeClient()
    c = make_app(fc)
    r = c.post("/api/deploy/start", json={"targetSha": "abc1234"})
    assert r.status_code == 200
    assert fc.start_calls == ["abc1234"]


def test_start_without_body_passes_no_target():
    fc = FakeClient()
    c = make_app(fc)
    assert c.post("/api/deploy/start").status_code == 200
    assert fc.start_calls == [None]


def test_start_unavailable_is_503():
    c = make_app(FakeClient(unavailable=True))
    assert c.post("/api/deploy/start").status_code == 503


def test_start_records_an_audit_event_naming_the_caller():
    """A deploy is a privileged, outward-facing action: record WHO asked for it.

    The route already resolves the caller via current_identity for authorisation;
    without this it was resolved and then discarded, so a completed deploy left no
    record of who triggered it.
    """
    c = make_app(FakeClient())
    assert c.post("/api/deploy/start", json={"targetSha": "abc1234"}).status_code == 200
    events = audits(c)
    assert len(events) == 1, f"expected exactly one audit event, got {len(events)}"
    ev = events[0]
    assert ev.type == "deploy_start"
    assert ev.identity == "tester"
    assert ev.detail["targetSha"] == "abc1234"
    assert ev.detail["started"] is True


def test_start_records_audit_even_when_the_controller_refuses():
    """A REFUSED deploy is exactly the event worth having a record of — a preflight
    rejection (e.g. CI verdict=pending) is how an unexpected trigger shows up."""
    c = make_app(FakeClient(start_ret=(200, {"started": False, "reason": "preflight: CI verdict=pending"})))
    assert c.post("/api/deploy/start").status_code == 200
    events = audits(c)
    assert len(events) == 1
    assert events[0].detail["started"] is False
    assert events[0].detail["reason"] == "preflight: CI verdict=pending"
    assert events[0].detail["targetSha"] is None


def test_start_records_audit_when_the_controller_is_unavailable():
    """The attempt still happened and still deserves a record."""
    c = make_app(FakeClient(unavailable=True))
    assert c.post("/api/deploy/start").status_code == 503
    events = audits(c)
    assert len(events) == 1
    assert events[0].detail["started"] is False
    assert events[0].detail["reason"] == "controller unavailable"
