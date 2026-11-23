"""``POST /api/pm/issues/labels/{add,remove}`` — the assistant's add_issue_labels /
remove_issue_labels (#3440).

The GitHub helpers are stubbed (``ci_issues.add_label`` / ``remove_label`` have their
own suite). Pinned here is what the ROUTE decides: per-ref refusals (PR, unmirrored),
per-label partial results, the hold-label refusal on remove, the audit row per ref —
and that the mirror's labels are written in the same request, so a ``hold:human``
applied here is a ``parked`` queue skip (``test_pm_queue``) without waiting for a sync.
"""
from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import select

from server.db import db_session
from server.db.models import WorkItem
from server.github import ci_issues
from server.pm.queue import PARKED_LABELS
from server.tests.test_pm_deps_api import IDENTITY_HEADERS, _audits, _client
from server.tests.test_pm_triage_writes_api import seed


def _labels(c, repo: str, number: int) -> list[str]:
    async def go():
        async with db_session() as db:
            wi = (await db.execute(select(WorkItem).where(
                WorkItem.repo == repo, WorkItem.number == number))).scalar_one()
            return list(wi.labels or [])
    return c.portal.call(go)


def _set_labels(c, repo: str, number: int, labels: list[str]) -> None:
    async def go():
        async with db_session() as db:
            wi = (await db.execute(select(WorkItem).where(
                WorkItem.repo == repo, WorkItem.number == number))).scalar_one()
            wi.labels = labels
            await db.commit()
    c.portal.call(go)


def test_add_labels_writes_github_and_the_mirror_per_ref_and_audits():
    with _client() as c:
        seed(c)
        _set_labels(c, "o/r", 6, ["P1"])
        calls: list[tuple] = []

        def add(repo, number, name, **_kw):
            calls.append((repo, number, name))
            return True
        with patch.object(ci_issues, "add_label", add):
            refs = ["o/r#5", "o/r#6", "o/r#8", "o/r#404"]
            r = c.post("/api/pm/issues/labels/add", headers=IDENTITY_HEADERS,
                       json={"refs": refs, "labels": ["hold:human", " p1 ", "HOLD:HUMAN"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["updated"], body["refused"]) == (2, 2)
        by_ref = {row["ref"]: row for row in body["results"]}
        assert "pull request" in by_ref["o/r#8"]["error"]
        assert "not a mirrored issue" in by_ref["o/r#404"]["error"]
        # De-duplicated case-insensitively; only mirrored issues reached GitHub.
        assert calls == [("o/r", 5, "hold:human"), ("o/r", 5, "p1"),
                         ("o/r", 6, "hold:human"), ("o/r", 6, "p1")]
        assert _labels(c, "o/r", 5) == ["hold:human", "p1"]
        assert _labels(c, "o/r", 6) == ["P1", "hold:human"]  # no second spelling of P1
        audits = _audits(c, "pm.label.add")
        assert [a["ref"] for a in audits] == refs
        assert audits[0]["labels"] == ["hold:human", "p1"]


def test_add_reports_a_label_github_refused_and_mirrors_only_the_rest():
    with _client() as c:
        seed(c)
        with patch.object(ci_issues, "add_label",
                          lambda repo, number, name, **_kw: name != "bad"):
            r = c.post("/api/pm/issues/labels/add", headers=IDENTITY_HEADERS,
                       json={"refs": ["o/r#5"], "labels": ["hold:human", "bad"]})
        row = r.json()["results"][0]
        assert row["ok"] is False and row["labels"] == ["hold:human"]
        assert row["failed"] == ["bad"] and "bad" in row["error"]
        assert _labels(c, "o/r", 5) == ["hold:human"]


def test_remove_labels_uses_the_issues_spelling_and_updates_the_mirror():
    with _client() as c:
        seed(c)
        _set_labels(c, "o/r", 5, ["Needs-Triage", "p1"])
        calls: list[tuple] = []

        def remove(repo, number, name, **_kw):
            calls.append((repo, number, name))
            return True
        with patch.object(ci_issues, "remove_label", remove):
            r = c.post("/api/pm/issues/labels/remove", headers=IDENTITY_HEADERS,
                       json={"refs": ["o/r#5"], "labels": ["needs-triage"]})
        assert r.json()["results"][0]["ok"] is True
        assert calls == [("o/r", 5, "Needs-Triage")]
        assert _labels(c, "o/r", 5) == ["p1"]
        assert _audits(c, "pm.label.remove")[0]["labels"] == ["needs-triage"]


def test_remove_refuses_a_hold_label_before_touching_github():
    with _client() as c:
        seed(c)
        with patch.object(ci_issues, "remove_label") as remove:
            for label in sorted(PARKED_LABELS):
                r = c.post("/api/pm/issues/labels/remove", headers=IDENTITY_HEADERS,
                           json={"refs": ["o/r#5"], "labels": ["p1", label.upper()]})
                assert r.status_code == 422, r.text
                assert "release_hold" in r.text
        remove.assert_not_called()


def test_request_validation():
    with _client() as c:
        seed(c)
        with patch.object(ci_issues, "add_label", return_value=True) as add:
            for path, payload in [
                ("add", {"refs": [f"o/r#{i}" for i in range(21)], "labels": ["x"]}),
                ("add", {"refs": ["o/r#5"], "labels": []}),
                ("add", {"refs": ["o/r#5"], "labels": [" "]}),
                ("add", {"refs": ["o/r#5"], "labels": ["x" * 51]}),
                ("add", {"refs": ["o/r#5"], "labels": [str(i) for i in range(11)]}),
            ]:
                r = c.post(f"/api/pm/issues/labels/{path}", headers=IDENTITY_HEADERS,
                           json=payload)
                assert r.status_code == 422, (payload, r.text)
            r = c.post("/api/pm/issues/labels/replace", headers=IDENTITY_HEADERS,
                       json={"refs": ["o/r#5"], "labels": ["x"]})
            assert r.status_code == 404
        add.assert_not_called()

