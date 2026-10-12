"""Bulk triage writes by ref (Refs #2979): ``POST /api/pm/board-status`` and
``POST /api/pm/issues/close``.

The orchestrator seams (``pm_board_status`` / ``pm_issue_close``) and the comment
helper are stubbed: their own suites pin the journal and the GitHub documents. What
is pinned here is what the ROUTE decides — per-ref refusals before any write, the
board's real Status options, comment-before-close, the audit row per ref, and the
request-level 422s (bulk cap, duplicate without duplicateOf).
"""
from __future__ import annotations

from unittest.mock import patch

from server.db import db_session
from server.db.models import GhProject, Project, ProjectRepo, WorkItem
from server.github import ci_issues
from server.tests.test_pm_deps_api import IDENTITY_HEADERS, _audits, _client

CACHE = {"Status": {"field_id": "F", "options": {"Backlog": "a", "Ready": "b", "Done": "c"}}}


def seed(c) -> dict[str, str]:
    async def go():
        async with db_session() as db:
            p = Project(slug="billing", name="billing", onboarding_state="live")
            db.add(p)
            await db.flush()
            db.add(ProjectRepo(project_id=p.id, repo="o/r", is_primary=True))
            db.add(GhProject(project_id=p.id, gh_node_id="PVT_1", field_cache=CACHE))
            rows = {
                "o/r#5": WorkItem(repo="o/r", number=5, kind="issue", title="Open", state="open"),
                "o/r#6": WorkItem(repo="o/r", number=6, kind="issue", title="Other", state="open"),
                "o/r#7": WorkItem(repo="o/r", number=7, kind="issue", title="Gone", state="closed"),
                "o/r#8": WorkItem(repo="o/r", number=8, kind="pr", title="A PR", state="open"),
                "o/r#9": WorkItem(repo="o/r", number=9, kind="issue", state="open",
                                  title="Nightly failed — 2026-10-09"),
                "x/y#1": WorkItem(repo="x/y", number=1, kind="issue", title="Orphan", state="open"),
            }
            db.add_all(rows.values())
            await db.flush()
            return {ref: w.id for ref, w in rows.items()}
    return c.portal.call(go)


def test_board_status_per_ref_results_refusals_and_audit():
    with _client() as c:
        ids = seed(c)
        calls = []

        async def seam(work_item_id, request):
            calls.append((work_item_id, request))
            return {"ok": True, "ref": "o/r#5", "status": "Ready", "previous": "Backlog",
                    "added": False, "changed": True}
        c.app.state.sessions.pm_board_status = seam
        refs = ["o/r#5", "o/r#7", "o/r#8", "o/r#9", "o/r#404", "x/y#1"]
        r = c.post("/api/pm/board-status", headers=IDENTITY_HEADERS,
                   json={"refs": refs, "status": "Ready"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["updated"], body["refused"]) == (1, 5)
        by_ref = {row["ref"]: row for row in body["results"]}
        assert by_ref["o/r#5"]["ok"] and by_ref["o/r#5"]["previous"] == "Backlog"
        assert "is closed" in by_ref["o/r#7"]["error"]
        assert "pull request" in by_ref["o/r#8"]["error"]
        assert "tracker" in by_ref["o/r#9"]["error"]
        assert "not a mirrored issue" in by_ref["o/r#404"]["error"]
        assert "no product" in by_ref["x/y#1"]["error"]
        # Only the one admissible ref reached the seam, with the add default on.
        assert calls == [(ids["o/r#5"], {"status": "Ready", "add_if_missing": True})]
        audits = _audits(c, "pm.workitem.board_status")
        assert [a["ref"] for a in audits] == refs
        assert [a["ok"] for a in audits] == [True] + [False] * 5


def test_board_status_unknown_option_names_the_real_board_options():
    with _client() as c:
        seed(c)
        calls = []

        async def seam(*args):
            calls.append(args)
            return {"ok": True}
        c.app.state.sessions.pm_board_status = seam
        r = c.post("/api/pm/board-status", headers=IDENTITY_HEADERS,
                   json={"refs": ["o/r#5"], "status": "Someday", "addIfMissing": False})
        row = r.json()["results"][0]
        assert row["ok"] is False and "Backlog, Ready, Done" in row["error"]
        assert calls == []


def test_board_status_passes_the_seams_refusal_through():
    with _client() as c:
        seed(c)

        async def seam(*args):
            return {"ok": False, "step": "not_on_board", "error": "o/r#5 is not on billing's board"}
        c.app.state.sessions.pm_board_status = seam
        r = c.post("/api/pm/board-status", headers=IDENTITY_HEADERS,
                   json={"refs": ["o/r#5"], "status": "Ready", "addIfMissing": False})
        assert r.json()["results"][0]["step"] == "not_on_board"
        assert _audits(c, "pm.workitem.board_status")[0]["step"] == "not_on_board"


def test_board_status_request_validation():
    with _client() as c:
        for payload in [{"refs": [], "status": "Ready"},
                        {"refs": [f"o/r#{i}" for i in range(21)], "status": "Ready"},
                        {"refs": ["o/r#5"], "status": ""}]:
            assert c.post("/api/pm/board-status", headers=IDENTITY_HEADERS,
                          json=payload).status_code == 422
        assert c.post("/api/pm/board-status", json={"refs": ["o/r#5"],
                                                    "status": "Ready"}).status_code == 401


def _comment(sink, result="https://github.com/o/r/issues/5#c1"):
    def fake(repo, number, body, *, automation=True, **kw):
        sink.append((repo, number, body, automation))
        return result
    return fake


def test_close_comments_then_closes_each_ref_and_audits():
    with _client() as c:
        ids = seed(c)
        calls, comments = [], []

        async def seam(work_item_id, request):
            calls.append((work_item_id, request))
            return {"ok": True, "item": {}}
        c.app.state.sessions.pm_issue_close = seam
        with patch.object(ci_issues, "comment_on_issue", _comment(comments)):
            r = c.post("/api/pm/issues/close", headers=IDENTITY_HEADERS, json={
                "refs": ["o/r#5", "o/r#7", "o/r#8"], "reason": "not_planned",
                "comment": "Not doing this."})
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["closed"], body["refused"]) == (1, 2)
        assert comments == [("o/r", 5, "Not doing this.", False)]
        assert calls == [(ids["o/r#5"], {"reason": "not_planned", "duplicate_of": None})]
        rows = {row["ref"]: row for row in body["results"]}
        assert rows["o/r#5"]["commentUrl"].endswith("#c1")
        assert "already closed" in rows["o/r#7"]["error"]
        assert "pull request" in rows["o/r#8"]["error"]
        audits = _audits(c, "pm.workitem.close")
        assert [(a["ref"], a["ok"]) for a in audits] == [
            ("o/r#5", True), ("o/r#7", False), ("o/r#8", False)]


def test_close_as_duplicate_passes_the_canonical_id_and_refuses_self():
    with _client() as c:
        ids = seed(c)
        calls = []

        async def seam(work_item_id, request):
            calls.append((work_item_id, request))
            return {"ok": True}
        c.app.state.sessions.pm_issue_close = seam
        with patch.object(ci_issues, "comment_on_issue", _comment([])):
            r = c.post("/api/pm/issues/close", headers=IDENTITY_HEADERS, json={
                "refs": ["o/r#5", "o/r#6"], "reason": "duplicate", "duplicateOf": "o/r#6",
                "comment": "Tracked in #6."})
        rows = {row["ref"]: row for row in r.json()["results"]}
        assert rows["o/r#5"]["ok"] and "itself" in rows["o/r#6"]["error"]
        assert calls == [(ids["o/r#5"], {"reason": "duplicate", "duplicate_of": ids["o/r#6"]})]


def test_close_rejected_comment_never_closes():
    with _client() as c:
        seed(c)
        calls = []

        async def seam(*args):
            calls.append(args)
            return {"ok": True}
        c.app.state.sessions.pm_issue_close = seam
        with patch.object(ci_issues, "comment_on_issue", _comment([], result=None)):
            r = c.post("/api/pm/issues/close", headers=IDENTITY_HEADERS, json={
                "refs": ["o/r#5"], "reason": "completed", "comment": "Done."})
        assert r.json()["results"][0]["step"] == "comment"
        assert calls == []
        assert _audits(c, "pm.workitem.close")[0]["ok"] is False


def test_close_request_validation():
    with _client() as c:
        seed(c)
        base = {"refs": ["o/r#5"], "comment": "c"}
        for payload in [{**base, "reason": "duplicate"},
                        {**base, "reason": "completed", "duplicateOf": "o/r#6"},
                        {**base, "reason": "wontfix"},
                        {**base, "reason": "completed", "comment": ""},
                        {**base, "reason": "completed",
                         "refs": [f"o/r#{i}" for i in range(21)]}]:
            assert c.post("/api/pm/issues/close", headers=IDENTITY_HEADERS,
                          json=payload).status_code == 422, payload
        r = c.post("/api/pm/issues/close", headers=IDENTITY_HEADERS,
                   json={**base, "reason": "duplicate", "duplicateOf": "o/r#8"})
        assert r.status_code == 422 and "pull request" in r.json()["detail"]["message"]
