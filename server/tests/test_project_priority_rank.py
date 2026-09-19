"""Spec 2026-09-19 §4: per-product priority rank, the person's first knob."""
from __future__ import annotations

from server.tests.test_pm_api import IDENTITY_HEADERS, _client, canonical_mutation_repositories  # noqa: F401


def _create(c, name: str, repo: str) -> dict:
    resp = c.post("/api/pm/projects", json={
        "name": name, "repos": [{"repo": repo, "isPrimary": True}],
    }, headers=IDENTITY_HEADERS)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_priority_rank_defaults_null_and_patches():
    with _client() as c:
        body = _create(c, "KinDash", "steiner385/KinDash")
        assert body["priorityRank"] is None
        resp = c.patch("/api/pm/projects/kindash", json={"priorityRank": 1}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        assert resp.json()["priorityRank"] == 1
        assert c.get("/api/pm/projects/kindash", headers=IDENTITY_HEADERS).json()["priorityRank"] == 1


def test_priority_rank_can_be_cleared_and_rejects_zero():
    with _client() as c:
        _create(c, "Web", "steiner385/web")
        c.patch("/api/pm/projects/web", json={"priorityRank": 2}, headers=IDENTITY_HEADERS)
        resp = c.patch("/api/pm/projects/web", json={"priorityRank": None}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 200 and resp.json()["priorityRank"] is None
        resp = c.patch("/api/pm/projects/web", json={"priorityRank": 0}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "bad_priority_rank"
