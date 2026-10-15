"""POST /api/pm/holds/release — the bulk release behind the drawer's release_hold.

Per-ref results, partial success reported honestly, and ONE owner rule
(``server.pm.holds.release_refusal``) for holds and the ``parked`` label alike."""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

import pytest

OPERATOR = "testuser@example.com"
IDENTITY_HEADERS = {"Tailscale-User-Login": OPERATOR}
GH_LOGIN = "steiner385"


@contextmanager
def _client():
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url, "VIBERPIT_DEV": "1",
                                     "VIBERPIT_TAILNET_ALLOWLIST": ""}):
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


def _assistant(c):
    """A loopback client carrying the drawer's ``ccas1:`` token for OPERATOR."""
    from fastapi.testclient import TestClient
    from server.assistant.token import mint_token
    lb = TestClient(c.app, client=("127.0.0.1", 5000))
    lb.portal = c.portal
    lb.headers["Authorization"] = f"Bearer {mint_token(OPERATOR, 'asst-sess-1')}"
    return lb


def _seed(c, *items: dict) -> None:
    from server.db import db_session
    from server.db.models import WorkItem

    async def _go() -> None:
        async with db_session() as db:
            for it in items:
                db.add(WorkItem(repo="o/r", kind="issue", state="open", title=f"t{it['number']}",
                                body="b", hold_reason="r" if it.get("hold_kind") else None, **it))
            await db.commit()
    c.portal.call(_go)


def _rows(c, number: int):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import AuditEvent, WorkItem

    async def _go():
        async with db_session() as db:
            wi = (await db.execute(select(WorkItem).where(WorkItem.number == number))).scalar_one()
            audits = (await db.execute(select(AuditEvent).where(AuditEvent.type.in_(
                ["pm.hold.release", "pm.label.remove"])))).scalars().all()
            return wi, audits
    return c.portal.call(_go)


@pytest.fixture
def gh(monkeypatch):
    """Fake GitHub: who applied each issue's parked label, the operator's login, and a
    record of every label removal."""
    state = {"placers": {}, "login": GH_LOGIN, "removed": [], "remove_ok": True}
    monkeypatch.setattr("server.github.ci_issues.label_placer",
                        lambda repo, n, name: state["placers"].get(n))
    monkeypatch.setattr("server.github.ci_issues.viewer_login", lambda: state["login"])

    def remove(repo, n, name):
        state["removed"].append((repo, n, name))
        return state["remove_ok"]
    monkeypatch.setattr("server.github.ci_issues.remove_label", remove)
    return state


def test_assistant_releases_system_holds_and_refuses_the_rest(gh):
    with _client() as c:
        _seed(c,
              {"number": 1, "hold_kind": "owner_action", "hold_by": "runner"},
              {"number": 2, "hold_kind": "evidence", "hold_by": "automation:error-triage"},
              {"number": 3, "hold_kind": "stronger_model", "hold_by": "runner"},
              {"number": 4, "hold_kind": "decision", "hold_by": "session:s1"},
              {"number": 5, "hold_kind": "owner_action", "hold_by": "someone@example.com"},
              {"number": 6, "hold_kind": "owner_action", "hold_by": OPERATOR},
              {"number": 7})
        refs = [f"o/r#{n}" for n in range(1, 8)] + ["o/r#99", "not-a-ref"]
        r = _assistant(c).post("/api/pm/holds/release", json={"refs": refs, "reason": "stale"})
        assert r.status_code == 200, r.text
        res = {x["ref"]: x for x in r.json()["results"]}
        assert [x["ref"] for x in r.json()["results"]] == refs
        assert res["o/r#1"] == {"ref": "o/r#1", "released": ["owner_action"], "refused": None}
        assert res["o/r#2"]["released"] == ["evidence"]
        assert res["o/r#3"]["released"] == ["stronger_model"]
        assert res["o/r#6"]["released"] == ["owner_action"]
        assert res["o/r#4"]["released"] == [] and "resolve_decision" in res["o/r#4"]["refused"]
        assert "someone@example.com" in res["o/r#5"]["refused"]
        assert "nothing to release" in res["o/r#7"]["refused"]
        assert "not a mirrored issue" in res["o/r#99"]["refused"]
        assert "owner/repo#123" in res["not-a-ref"]["refused"]
        assert r.json()["releasedCount"] == 4 and r.json()["refusedCount"] == 5

        wi, audits = _rows(c, 1)
        assert wi.hold_kind is None
        assert _rows(c, 5)[0].hold_kind == "owner_action"
        assert _rows(c, 4)[0].hold_kind == "decision"
        holds = [a for a in audits if a.type == "pm.hold.release"]
        assert len(holds) == 4
        assert {a.detail["reason"] for a in holds} == {"stale"}
        assert {a.via for a in holds} == {"assistant"}
        assert {a.detail["actorClass"] for a in holds} == {"agent"}
        assert gh["removed"] == []


def test_a_person_keeps_todays_rule(gh):
    """Only the assistant is narrowed by placer; a person may release another's hold."""
    with _client() as c:
        _seed(c, {"number": 5, "hold_kind": "owner_action", "hold_by": "someone@example.com"})
        r = c.post("/api/pm/holds/release", json={"refs": ["o/r#5"], "reason": "mine now"},
                   headers=IDENTITY_HEADERS)
        assert r.json()["results"][0]["released"] == ["owner_action"]


def test_parked_label_follows_the_owner_rule(gh):
    gh["placers"] = {1: GH_LOGIN, 2: "someone-else", 3: None, 4: "renovate[bot]"}
    with _client() as c:
        _seed(c, *({"number": n, "labels": ["bug", "Parked"]} for n in (1, 2, 3, 4)))
        r = _assistant(c).post("/api/pm/holds/release",
                               json={"refs": ["o/r#1", "o/r#2", "o/r#3", "o/r#4"],
                                     "reason": "ready to go"})
        res = {x["ref"]: x for x in r.json()["results"]}
        assert res["o/r#1"]["released"] == ["parked"]
        assert res["o/r#4"]["released"] == ["parked"]
        assert "someone-else" in res["o/r#2"]["refused"]
        assert "not recorded" in res["o/r#3"]["refused"]
        # Removed with the issue's own spelling, and the mirror reflects it at once.
        assert gh["removed"] == [("o/r", 1, "Parked"), ("o/r", 4, "Parked")]
        wi, audits = _rows(c, 1)
        assert wi.labels == ["bug"]
        assert _rows(c, 2)[0].labels == ["bug", "Parked"]
        labels = [a for a in audits if a.type == "pm.label.remove"]
        assert [(a.detail["label"], a.detail["placedBy"], a.detail["reason"]) for a in labels] == [
            ("Parked", GH_LOGIN, "ready to go"), ("Parked", "renovate[bot]", "ready to go")]


def test_parked_label_refused_when_the_operators_login_is_unknown(gh):
    gh["placers"], gh["login"] = {1: GH_LOGIN}, None
    with _client() as c:
        _seed(c, {"number": 1, "labels": ["parked"]})
        r = _assistant(c).post("/api/pm/holds/release", json={"refs": ["o/r#1"], "reason": "x"})
        assert "GitHub login" in r.json()["results"][0]["refused"]
        assert gh["removed"] == []


def test_a_refused_half_releases_nothing(gh):
    """A hold the operator may release on an issue someone else parked: nothing changes."""
    gh["placers"] = {1: "someone-else"}
    with _client() as c:
        _seed(c, {"number": 1, "labels": ["parked"], "hold_kind": "owner_action",
                  "hold_by": "runner"})
        r = _assistant(c).post("/api/pm/holds/release", json={"refs": ["o/r#1"], "reason": "x"})
        assert r.json()["results"][0]["released"] == []
        assert _rows(c, 1)[0].hold_kind == "owner_action"
        assert gh["removed"] == []


def test_a_failed_label_removal_leaves_the_hold_and_says_so(gh):
    gh["placers"], gh["remove_ok"] = {1: GH_LOGIN}, False
    with _client() as c:
        _seed(c, {"number": 1, "labels": ["parked"], "hold_kind": "owner_action",
                  "hold_by": "runner"})
        r = _assistant(c).post("/api/pm/holds/release", json={"refs": ["o/r#1"], "reason": "x"})
        out = r.json()["results"][0]
        assert out["released"] == [] and "GitHub" in out["refused"]
        assert _rows(c, 1)[0].hold_kind == "owner_action"


@pytest.mark.parametrize("body", [
    {"refs": [f"o/r#{n}" for n in range(21)], "reason": "x"},
    {"refs": [], "reason": "x"},
    {"refs": ["o/r#1"], "reason": ""},
    {"refs": ["o/r#1"]},
])
def test_bad_requests_are_rejected_whole(body):
    with _client() as c:
        r = c.post("/api/pm/holds/release", json=body, headers=IDENTITY_HEADERS)
        assert r.status_code == 422, r.text


# --- #3106: a released hold also moves its card out of Held ------------------------
CACHE = {"Status": {"field_id": "F", "options": {
    "Backlog": "a", "Ready": "b", "In progress": "c", "Blocked": "d", "Held": "e", "Done": "f"}}}


def _seed_board(c, *items: dict, policy: dict | None = None) -> None:
    """A live product owning o/r with a mirrored board; each item's ``board`` is the
    card's mirrored Status, and ``blocked_by`` an open issue it depends on."""
    from server.db import db_session
    from server.db.models import (GhProject, GhProjectItem, Project, ProjectRepo, WorkItem,
                                  WorkItemDep)

    async def _go() -> None:
        async with db_session() as db:
            p = Project(slug="billing", name="billing", onboarding_state="live",
                        automation_policy=policy)
            db.add(p)
            await db.flush()
            db.add(ProjectRepo(project_id=p.id, repo="o/r", is_primary=True))
            gh = GhProject(project_id=p.id, gh_node_id="PVT_1", field_cache=CACHE)
            db.add(gh)
            await db.flush()
            for it in items:
                it = dict(it)
                board, blocker = it.pop("board", None), it.pop("blocked_by", None)
                wi = WorkItem(repo="o/r", kind="issue", state="open", title=f"t{it['number']}",
                              body="b", gh_node_id=f"I_{it['number']}",
                              hold_reason="r" if it.get("hold_kind") else None, **it)
                db.add(wi)
                await db.flush()
                if board is not None:
                    db.add(GhProjectItem(gh_project_id=gh.id, work_item_id=wi.id,
                                         gh_item_node_id=f"PVTI_{it['number']}", status=board))
                if blocker is not None:
                    db.add(WorkItem(repo="o/r", kind="issue", state="open", number=blocker,
                                    title="blocker", body="b"))
                    db.add(WorkItemDep(src_ref=f"o/r#{it['number']}", dst_ref=f"o/r#{blocker}",
                                       source="native", active=True))
            await db.commit()
    c.portal.call(_go)


def _board_seam(c, *, ok: bool = True) -> list:
    """Stub the orchestrator's board-status seam; record each call and the hold the
    item carries AT the call (the seam reads its own session, so the release must
    already be committed or ``board_status.refusal`` would refuse a still-held item)."""
    from server.db import db_session
    from server.db.models import WorkItem
    calls: list = []

    async def seam(work_item_id: str, request: dict) -> dict:
        async with db_session() as db:
            wi = await db.get(WorkItem, work_item_id)
            assert wi is not None
            hold = wi.hold_kind
        calls.append((work_item_id, request, hold))
        if not ok:
            return {"ok": False, "step": "mutate", "error": "GitHub said no"}
        return {"ok": True, "status": request["status"], "previous": "Held",
                "added": False, "changed": True}
    c.app.state.sessions.pm_board_status = seam
    return calls


def test_releasing_a_held_card_moves_it_to_the_queue_min_status(gh):
    with _client() as c:
        _seed_board(c, {"number": 1, "hold_kind": "owner_action", "hold_by": "runner",
                        "board": "Held"})
        calls = _board_seam(c)
        r = _assistant(c).post("/api/pm/holds/release", json={"refs": ["o/r#1"], "reason": "go"})
        out = r.json()["results"][0]
        assert out["released"] == ["owner_action"]
        assert out["board"] == {"from": "Held", "to": "Ready", "moved": True, "error": None}
        assert [(req["status"], hold) for _, req, hold in calls] == [("Ready", None)]


def test_the_target_column_is_the_products_queue_min_status(gh):
    with _client() as c:
        _seed_board(c, {"number": 1, "hold_kind": "owner_action", "hold_by": "runner",
                        "board": "Held"}, policy={"queue_min_status": "Backlog"})
        calls = _board_seam(c)
        r = _assistant(c).post("/api/pm/holds/release", json={"refs": ["o/r#1"], "reason": "go"})
        assert r.json()["results"][0]["board"]["to"] == "Backlog"
        assert [req["status"] for _, req, _ in calls] == ["Backlog"]


def test_a_released_card_with_an_open_blocker_moves_to_blocked(gh):
    """Ready would be a lie the reconciler could not correct: our write is stamped now,
    newer than the dependency edge that derives Blocked."""
    with _client() as c:
        _seed_board(c, {"number": 1, "hold_kind": "owner_action", "hold_by": "runner",
                        "board": "Held", "blocked_by": 50})
        calls = _board_seam(c)
        r = _assistant(c).post("/api/pm/holds/release", json={"refs": ["o/r#1"], "reason": "go"})
        assert r.json()["results"][0]["board"]["to"] == "Blocked"
        assert [req["status"] for _, req, _ in calls] == ["Blocked"]


def test_a_card_not_in_held_is_left_where_it_is(gh):
    with _client() as c:
        _seed_board(c, {"number": 1, "hold_kind": "evidence", "hold_by": "runner",
                        "board": "Ready"},
                    {"number": 2, "hold_kind": "owner_action", "hold_by": "runner"})
        calls = _board_seam(c)
        r = _assistant(c).post("/api/pm/holds/release",
                               json={"refs": ["o/r#1", "o/r#2"], "reason": "go"})
        assert [x["released"] for x in r.json()["results"]] == [["evidence"], ["owner_action"]]
        assert all("board" not in x for x in r.json()["results"])
        assert calls == []


def test_a_failed_board_move_is_reported_and_the_release_stands(gh):
    with _client() as c:
        _seed_board(c, {"number": 1, "hold_kind": "owner_action", "hold_by": "runner",
                        "board": "Held"})
        _board_seam(c, ok=False)
        r = _assistant(c).post("/api/pm/holds/release", json={"refs": ["o/r#1"], "reason": "go"})
        out = r.json()["results"][0]
        assert out["released"] == ["owner_action"] and out["refused"] is None
        assert out["board"] == {"from": "Held", "to": "Ready", "moved": False,
                                "error": "GitHub said no"}
        assert _rows(c, 1)[0].hold_kind is None
