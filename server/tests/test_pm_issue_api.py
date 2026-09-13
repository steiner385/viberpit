"""Authenticated issue review and decision routes use the orchestrator seam."""
import pytest

from server.db import db_session
from server.db.models import WorkItem
from server.tests.test_pm_deps_api import IDENTITY_HEADERS, _audits, _client


def seed(c, kind="issue"):
    async def go():
        async with db_session() as db:
            row = WorkItem(repo="o/r", number=5, kind=kind, title="Issue", body="Full description",
                           state="open", gh_node_id="N5")
            db.add(row)
            await db.flush()
            return row.id
    return c.portal.call(go)


def test_review_and_decide_in_app():
    with _client() as c:
        wid = seed(c)
        path = f"/api/pm/work-items/{wid}/issue"
        response = c.get(path, headers=IDENTITY_HEADERS)
        assert response.status_code == 200
        item = response.json()
        assert item["body"] == "Full description"
        calls = []

        async def decision(work_item_id, request):
            calls.append((work_item_id, request))
            return {"ok": True, "item": {**item, "state": "closed"}}
        c.app.state.sessions.pm_issue_decision = decision
        assert c.post(path, headers=IDENTITY_HEADERS, json={"action": "dismiss", "version": item["version"]}).status_code == 200
        assert calls[0][0] == wid and calls[0][1]["action"] == "dismiss"
        assert _audits(c, "pm.workitem.issue_decision")[0]["ok"]
        assert c.get(path).status_code == 401
        assert c.post(path, json={"action": "resolve", "version": item["version"]}).status_code == 401


@pytest.mark.parametrize("step,code", [("conflict", 409), ("mutate", 502), ("rpc", 502), ("load", 404)])
def test_failed_decision_audited(step, code):
    with _client() as c:
        wid = seed(c)
        async def refuse(*args):
            return {"ok": False, "step": step, "error": "Reload and try again"}
        c.app.state.sessions.pm_issue_decision = refuse
        response = c.post(f"/api/pm/work-items/{wid}/issue", headers=IDENTITY_HEADERS,
                          json={"action": "resolve", "version": "v"})
        assert response.status_code == code
        assert response.json()["detail"]["message"] == "Reload and try again"
        assert not _audits(c, "pm.workitem.issue_decision")[0]["ok"]


def test_invalid_decision_or_non_issue_rejected():
    with _client() as c:
        wid = seed(c)
        path = f"/api/pm/work-items/{wid}/issue"
        for payload in [{"action": "edit", "version": "v"}, {"action": "delete", "version": "v"},
                        {"action": "edit", "title": " ", "body": "", "version": "v"}]:
            assert c.post(path, headers=IDENTITY_HEADERS, json=payload).status_code == 422
        for missing in ["nope", seed(c, "pr")]:
            path = f"/api/pm/work-items/{missing}/issue"
            assert c.get(path, headers=IDENTITY_HEADERS).status_code == 404
            assert c.post(path, headers=IDENTITY_HEADERS, json={"action": "resolve", "version": "v"}).status_code == 404
