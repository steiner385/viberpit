"""API tests for /api/assistant/watches (spec v2 §4.7, Task 8.3): create/list/delete a
proactive watch, the per-identity cap, and the source-walking guard on
server/assistant/watches.py's condition→event_class map."""
from __future__ import annotations

import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env

IDENTITY = {"Tailscale-User-Login": "testuser@example.com"}
OTHER_IDENTITY = {"Tailscale-User-Login": "otheruser@example.com"}


@contextmanager
def _client(env=None):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    from fastapi.testclient import TestClient

    with temp_env(), tempfile.TemporaryDirectory() as d:
        db_url = f"sqlite+aiosqlite:///{Path(d) / 'watches.db'}"
        full_env = {"CCSWITCH_DB_URL": db_url, **(env or {})}
        with patch.dict(os.environ, full_env):
            orch = SessionOrchestrator(poll_interval=9999.0)
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


# --------------------------------------------------------------------------- #
# Create / list / delete                                                     #
# --------------------------------------------------------------------------- #
def test_create_pr_watch():
    with _client() as c:
        r = c.post("/api/assistant/watches",
                   json={"kind": "pr", "target": "acme/checkout", "condition": "breaks"},
                   headers=IDENTITY)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["eventClass"] == "pr_broke"
        assert body["scopeRepo"] == "acme/checkout"
        assert body["scopeAutomation"] is None
        assert body["once"] is True
        assert body["ownerSessionId"] is None  # no assistant-session context on this path
        assert body["expiresAt"]
        assert body["id"]


def test_create_automation_watch_error_and_warn():
    with _client() as c:
        for condition, expected_class in (("error", "automation_error"), ("warn", "automation_warn")):
            r = c.post("/api/assistant/watches",
                       json={"kind": "automation", "target": "backlog-runner",
                             "condition": condition},
                       headers=IDENTITY)
            assert r.status_code == 201, r.text
            assert r.json()["eventClass"] == expected_class
            assert r.json()["scopeAutomation"] == "backlog-runner"


def test_create_watch_rejects_unimplemented_kinds():
    with _client() as c:
        for kind, condition in (("session", "finishes"), ("account", "limited")):
            r = c.post("/api/assistant/watches",
                       json={"kind": kind, "target": "x", "condition": condition},
                       headers=IDENTITY)
            assert r.status_code == 422, (kind, r.text)
            assert r.json()["error"]["code"] == "WATCH_CONDITION_UNSUPPORTED"


def test_create_watch_rejects_unknown_condition_for_a_known_kind():
    with _client() as c:
        r = c.post("/api/assistant/watches",
                   json={"kind": "pr", "target": "acme/checkout", "condition": "merges"},
                   headers=IDENTITY)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "WATCH_CONDITION_UNSUPPORTED"


def test_create_watch_rejects_non_positive_ttl():
    with _client() as c:
        r = c.post("/api/assistant/watches",
                   json={"kind": "pr", "target": "o/n", "condition": "breaks", "ttl_hours": 0},
                   headers=IDENTITY)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_TTL"


def test_watch_once_and_ttl_are_honored():
    with _client() as c:
        r = c.post("/api/assistant/watches",
                   json={"kind": "pr", "target": "o/n", "condition": "breaks",
                         "once": False, "ttl_hours": 1},
                   headers=IDENTITY)
        assert r.status_code == 201
        assert r.json()["once"] is False


def test_list_watches_is_scoped_to_the_identity():
    with _client() as c:
        c.post("/api/assistant/watches",
              json={"kind": "pr", "target": "o/n", "condition": "breaks"}, headers=IDENTITY)
        c.post("/api/assistant/watches",
              json={"kind": "automation", "target": "cicd-doctor", "condition": "error"},
              headers=OTHER_IDENTITY)

        mine = c.get("/api/assistant/watches", headers=IDENTITY).json()
        assert len(mine) == 1 and mine[0]["scopeRepo"] == "o/n"

        theirs = c.get("/api/assistant/watches", headers=OTHER_IDENTITY).json()
        assert len(theirs) == 1 and theirs[0]["scopeAutomation"] == "cicd-doctor"


def test_list_watches_empty_before_any_created():
    with _client() as c:
        assert c.get("/api/assistant/watches", headers=IDENTITY).json() == []


def test_delete_watch():
    with _client() as c:
        created = c.post("/api/assistant/watches",
                         json={"kind": "pr", "target": "o/n", "condition": "breaks"},
                         headers=IDENTITY).json()
        d = c.delete(f"/api/assistant/watches/{created['id']}", headers=IDENTITY)
        assert d.status_code == 200 and d.json() == {"ok": True}
        assert c.get("/api/assistant/watches", headers=IDENTITY).json() == []


def test_delete_watch_404_for_unknown_id():
    with _client() as c:
        r = c.delete("/api/assistant/watches/does-not-exist", headers=IDENTITY)
        assert r.status_code == 404


def test_delete_watch_404_for_someone_elses_watch():
    with _client() as c:
        created = c.post("/api/assistant/watches",
                         json={"kind": "pr", "target": "o/n", "condition": "breaks"},
                         headers=IDENTITY).json()
        r = c.delete(f"/api/assistant/watches/{created['id']}", headers=OTHER_IDENTITY)
        assert r.status_code == 404
        # Untouched: the owner can still see and delete it.
        assert len(c.get("/api/assistant/watches", headers=IDENTITY).json()) == 1


def test_watch_cap_is_per_identity():
    with _client(env={"CCSWITCH_ASSISTANT_WATCH_MAX_PER_IDENTITY": "2"}) as c:
        for i in range(2):
            r = c.post("/api/assistant/watches",
                       json={"kind": "automation", "target": f"job-{i}", "condition": "error"},
                       headers=IDENTITY)
            assert r.status_code == 201, r.text
        over = c.post("/api/assistant/watches",
                      json={"kind": "automation", "target": "job-3", "condition": "error"},
                      headers=IDENTITY)
        assert over.status_code == 409
        assert over.json()["error"]["code"] == "WATCH_CAP_EXCEEDED"

        # The cap is per identity: a different caller is unaffected.
        theirs = c.post("/api/assistant/watches",
                        json={"kind": "automation", "target": "job-4", "condition": "error"},
                        headers=OTHER_IDENTITY)
        assert theirs.status_code == 201


def test_watch_route_allowed_for_the_assistant_token():
    """The `/api/assistant/*` recursion guard blocks every route under that prefix for a
    `ccas1:` caller EXCEPT watches (spec v2 §4.7) — a watch belongs to the thread that
    created it. Also asserts owner_session_id is threaded from the token's claims."""
    from server.assistant.token import mint_token
    from fastapi.testclient import TestClient

    with _client() as c:
        tok = mint_token("testuser@example.com", "asst-sess-1")
        loopback = TestClient(c.app, client=("127.0.0.1", 5000))
        r = loopback.post("/api/assistant/watches",
                          json={"kind": "pr", "target": "o/n", "condition": "breaks"},
                          headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 201, r.text
        assert r.json()["ownerSessionId"] == "asst-sess-1"

        # A route this prefix does NOT allowlist (e.g. /status) still 403s for the token.
        blocked = loopback.get("/api/assistant/status", headers={"Authorization": f"Bearer {tok}"})
        assert blocked.status_code == 403


# --------------------------------------------------------------------------- #
# CONDITION_MAP must only reference event classes real code actually emits    #
# --------------------------------------------------------------------------- #
def _emitted_event_classes() -> set[str]:
    """Every EVENT_CLASSES member that appears as a string literal near a real
    `notify_event(...)` call site in server/ (excluding tests). Source-walking, same
    pattern as server/tests/test_assistant_loop_guard.py — not a full AST parse, but
    precise enough: EVENT_CLASSES has only 6 members, so an incidental match of one of
    those exact tokens elsewhere in a 200-char window is not a realistic false positive.
    """
    from server.sessions.notify_rules import EVENT_CLASSES

    root = REPO_ROOT / "server"
    call = re.compile(r"notify_event\(")
    literal = re.compile(r'"([a-z_]+)"')
    found: set[str] = set()
    for p in root.rglob("*.py"):
        rel = str(p.relative_to(REPO_ROOT))
        if "/tests/" in rel:
            continue
        text = p.read_text()
        for m in call.finditer(text):
            window = text[m.end(): m.end() + 200]
            found |= set(literal.findall(window)) & EVENT_CLASSES
    return found


def test_watch_conditions_are_actually_emitted():
    """server/assistant/watches.py's CONDITION_MAP must never map a kind/condition to an
    event_class that no real notify_event(...) call site emits — that would be a watch
    that looks wired up but can never fire. Extend both together (see the module's
    docstring for why 'session'/'account' kinds are absent)."""
    from server.assistant import watches as watch_conditions

    mapped = {event_class for by_condition in watch_conditions.CONDITION_MAP.values()
             for event_class, _ in by_condition.values()}
    assert mapped, "CONDITION_MAP should not be empty"
    emitted = _emitted_event_classes()
    missing = mapped - emitted
    assert not missing, f"watch conditions reference event classes nobody emits: {missing}"
