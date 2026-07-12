"""Ready + auto-merge action (plotroom PrRow parity).

POST /api/pipeline/pr/ready-merge marks a draft PR ready and arms squash
auto-merge. Auth approach mirrors test_pipeline_route.py: create_app() with
CCSWITCH_DEV=1 and empty allowlist, swap app.state.sessions for a fake, and
drive with httpx AsyncClient/ASGITransport.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}

_ENV = {
    "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    "CCSWITCH_DEV": "1",
    "CCSWITCH_TAILNET_ALLOWLIST": "",
}


def _make_app():
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
            usage_fn=lambda name: None,
        )
        return create_app(orchestrator=orch)


@pytest.mark.asyncio
async def test_ready_merge_route_calls_orchestrator():
    app = _make_app()
    calls = []

    async def fake_ready_merge(repo, number):
        calls.append((repo, number))
        return {"markedReady": True, "autoMergeArmed": True}

    app.state.sessions.ready_merge = fake_ready_merge

    with patch.dict(os.environ, _ENV):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/pipeline/pr/ready-merge",
                json={"repo": "o/r", "number": 7},
                headers=_H,
            )

    assert resp.status_code == 200
    assert resp.json() == {"markedReady": True, "autoMergeArmed": True}
    assert calls == [("o/r", 7)]


@pytest.mark.asyncio
async def test_ready_merge_validates_body():
    """Missing 'number' field -> 422 Unprocessable Entity."""
    app = _make_app()

    with patch.dict(os.environ, _ENV):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/pipeline/pr/ready-merge",
                json={"repo": "o/r"},
                headers=_H,
            )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ready_merge_requires_identity():
    """No identity header -> 401."""
    app = _make_app()

    with patch.dict(os.environ, _ENV):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/pipeline/pr/ready-merge",
                json={"repo": "o/r", "number": 7},
            )

    assert resp.status_code == 401


def _orch() -> SessionOrchestrator:
    with patch.dict(os.environ, _ENV):
        return SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
            usage_fn=lambda name: None,
        )


@pytest.mark.asyncio
async def test_ready_merge_orchestrator_happy_path():
    """Draft PR: gh marks it ready and arms auto-merge; both flags True."""
    import subprocess

    orch = _orch()
    calls: list[list[str]] = []

    def fake_gh(args, token=None, *, input=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    orch._gh_run = fake_gh
    result = await orch.ready_merge("o/r", 7)
    assert result == {"markedReady": True, "autoMergeArmed": True}
    assert calls == [
        ["pr", "ready", "7", "-R", "o/r"],
        ["pr", "merge", "7", "-R", "o/r", "--squash", "--auto"],
    ]


@pytest.mark.asyncio
async def test_ready_merge_orchestrator_tolerates_already_ready_and_armed():
    """Non-draft PR with auto-merge already on: markedReady False, armed True

    (gh 2.x stderr: '...auto-merge is already enabled for this pull request')."""
    import subprocess

    orch = _orch()

    def fake_gh(args, token=None, *, input=None):
        if args[:2] == ["pr", "ready"]:
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="PR #7 is not a draft"
            )
        return subprocess.CompletedProcess(
            args, 1, stdout="",
            stderr="auto-merge is already enabled for this pull request",
        )

    orch._gh_run = fake_gh
    result = await orch.ready_merge("o/r", 7)
    assert result == {"markedReady": False, "autoMergeArmed": True}


@pytest.mark.asyncio
async def test_ready_merge_orchestrator_merge_failure_reports_unarmed():
    """A genuine gh failure (e.g. auto-merge disallowed on the repo) → armed False."""
    import subprocess

    orch = _orch()

    def fake_gh(args, token=None, *, input=None):
        code = 0 if args[:2] == ["pr", "ready"] else 1
        return subprocess.CompletedProcess(
            args, code, stdout="",
            stderr="" if code == 0 else "auto-merge is not allowed for this repository",
        )

    orch._gh_run = fake_gh
    result = await orch.ready_merge("o/r", 7)
    assert result == {"markedReady": True, "autoMergeArmed": False}
