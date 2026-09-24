"""Route + orchestrator tests: GET /api/prs/{owner}/{name}/{number} — one PR's
detail view (SPEC v2 Pack G, Task 7.5). Route harness mirrors
test_pipeline_route.py; orchestrator harness mirrors test_ready_merge.py."""
from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}

_ENV = {
    "VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:",
    "VIBERPIT_DEV": "1",
    "VIBERPIT_TAILNET_ALLOWLIST": "",
}


class _FakeSessions:
    def __init__(self):
        self.pr_detail_result: dict | None = None
        self.calls: list[tuple[str, int]] = []

    async def pr_detail(self, repo: str, number: int):
        self.calls.append((repo, number))
        return self.pr_detail_result


def _make_client():
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
            usage_fn=lambda name: None,
        )
        app = create_app(orchestrator=orch)
    fake = _FakeSessions()
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


def test_get_pr_detail_requires_identity():
    client, _ = _make_client()
    with patch.dict(os.environ, _ENV), client:
        resp = client.get("/api/prs/acme/billing/42")
    assert resp.status_code == 401


def test_get_pr_detail_returns_the_orchestrator_payload():
    client, fake = _make_client()
    fake.pr_detail_result = {
        "number": 42, "repo": "acme/billing", "session": {"id": "s1", "name": "Fix it"},
        "sessionId": "s1",
        "checks": [{"name": "lint", "status": "COMPLETED", "conclusion": "success",
                    "url": "https://example/lint"}],
        "reviews": [{"author": "alice", "state": "APPROVED"}],
        "mergeable": "MERGEABLE",
    }
    with patch.dict(os.environ, _ENV), client:
        resp = client.get("/api/prs/acme/billing/42", headers=_H)
    assert resp.status_code == 200
    assert resp.json() == fake.pr_detail_result
    assert fake.calls == [("acme/billing", 42)]


def test_get_pr_detail_404_when_orchestrator_returns_none():
    client, fake = _make_client()
    fake.pr_detail_result = None
    with patch.dict(os.environ, _ENV), client:
        resp = client.get("/api/prs/acme/billing/999", headers=_H)
    assert resp.status_code == 404


def test_get_pr_detail_502_with_reason_when_repo_sync_is_failing():
    """A repo whose population refresh is failing has no cache to find the PR in, so
    "not found" would be a lie — say why instead of a bare, reasonless 404 (the
    cairnea/KinDash#16934 symptom)."""
    client, fake = _make_client()
    fake.pr_detail_result = {"pr_not_synced": True, "syncError": "gh: HTTP 504"}
    with patch.dict(os.environ, _ENV), client:
        resp = client.get("/api/prs/cairnea/KinDash/16934", headers=_H)
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "cairnea/KinDash" in detail
    assert "gh: HTTP 504" in detail


def test_get_pr_detail_does_not_shadow_the_list_route():
    """GET /api/prs (no path segments) must still hit list_prs, not pr_detail."""
    client, fake = _make_client()
    fake.all_prs_result = []  # only meaningful if pr_detail is (wrongly) reached

    async def _all_prs():
        return []
    fake.all_prs = _all_prs
    with patch.dict(os.environ, _ENV), client:
        resp = client.get("/api/prs", headers=_H)
    assert resp.status_code == 200
    assert resp.json() == []
    assert fake.calls == []


# --- orchestrator.pr_detail: composes all_prs() with a live checks/reviews fetch ---


def _orch() -> SessionOrchestrator:
    with patch.dict(os.environ, _ENV):
        return SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
            usage_fn=lambda name: None,
        )


def _population_pr(*, session_id=None, session_name=None, **kw):
    from server.github.pr_status import PipelinePr, PullRequestInfo
    defaults = {"number": 42, "url": "u", "state": "open", "checks": "passing",
                "title": "t", "repo": "o/r"}
    defaults.update(kw)
    return PipelinePr(pr=PullRequestInfo(**defaults),
                      session_id=session_id, session_name=session_name)


@pytest.mark.asyncio
async def test_pr_detail_composes_all_prs_entry_with_live_fetch():
    orch = _orch()
    pr = _population_pr(number=42, repo="o/r", session_id="s1", session_name="Fix it")
    orch.all_prs = AsyncMock(return_value=[pr])

    def fake_gh(args, token=None, *, timeout=None):
        assert "pullRequest(number: 42)" in args[-1]
        payload = {"data": {"repository": {"pullRequest": {
            "mergeable": "MERGEABLE",
            "reviews": {"nodes": [{"author": {"login": "alice"}, "state": "APPROVED"}]},
            "commits": {"nodes": []},
        }}}}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    orch._gh_run = fake_gh
    result = await orch.pr_detail("o/r", 42)
    assert result["number"] == 42
    assert result["session"] == {"id": "s1", "name": "Fix it"}
    assert result["sessionId"] == "s1"
    assert result["reviews"] == [{"author": "alice", "state": "APPROVED"}]
    assert result["mergeable"] == "MERGEABLE"
    assert result["checks"] == []


@pytest.mark.asyncio
async def test_pr_detail_returns_none_when_pr_not_in_population():
    orch = _orch()
    orch.all_prs = AsyncMock(return_value=[])
    assert await orch.pr_detail("o/r", 999) is None


@pytest.mark.asyncio
async def test_pr_detail_degrades_to_empty_when_live_fetch_fails():
    """A gh hiccup on the live half must not blank the cached half."""
    orch = _orch()
    orch.all_prs = AsyncMock(return_value=[_population_pr(number=1, repo="o/r")])

    def fake_gh(args, token=None, *, timeout=None):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")

    orch._gh_run = fake_gh
    result = await orch.pr_detail("o/r", 1)
    assert result["number"] == 1
    assert result["checks"] == [] and result["reviews"] == [] and result["mergeable"] is None


@pytest.mark.asyncio
async def test_pr_detail_flags_sync_failure_instead_of_none():
    """Not in the cache AND the repo's last refresh failed → the marker dict the
    route turns into a 502, not None (which it would 404)."""
    orch = _orch()
    orch.all_prs = AsyncMock(return_value=[])
    orch._pr_population._last_error["o/r"] = "gh: HTTP 504"
    assert await orch.pr_detail("o/r", 999) == {
        "pr_not_synced": True, "syncError": "gh: HTTP 504"}
    # A different, healthy repo is still a plain not-found.
    assert await orch.pr_detail("o/healthy", 999) is None


@pytest.mark.asyncio
async def test_all_repos_stage_models_includes_repo_that_never_synced():
    """A watched repo that has never fetched successfully has no population entry,
    so it used to have no StageModel either — invisible on /pipeline. It now shows
    up empty, carrying the reason."""
    orch = _orch()
    orch._pr_population._last_error["cairnea/KinDash"] = "gh: HTTP 504"
    models = await orch.all_repos_stage_models()
    m = models["cairnea/KinDash"]
    assert m.nodes == ()
    assert m.sync_error == "gh: HTTP 504"
    assert m.to_dict()["syncError"] == "gh: HTTP 504"
