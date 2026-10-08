"""Tests for POST /api/sessions/batch/resume.

Mirrors test_send_prompt_route.py: creates a real SessionOrchestrator (for
lifespan / DB init), then replaces app.state.sessions with a lightweight fake
that stubs resume_interrupted.  VIBERPIT_DEV=1 + empty allowlist = no auth
friction.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from types import SimpleNamespace

from server.app import create_app
from server.ipc.client import DaemonTimeout
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}

_ENV = {
    "VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:",
    "VIBERPIT_DEV": "1",
    "VIBERPIT_TAILNET_ALLOWLIST": "",
}


class _FakeSessions:
    """Minimal sessions facade for the batch-resume route.

    resume_map: {session_id -> result_string}
    Returns self.resume_map.get(sid, "skipped:not-found") for any call.
    """

    def __init__(self):
        self.resume_map: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.timeouts: set[str] = set()
        self.still_interrupted = False

    async def resume_interrupted(self, sid: str, identity: str = "system") -> str:
        self.calls.append((sid, identity))
        if sid in self.timeouts:
            raise DaemonTimeout("resume_interrupted timed out: ", outcome_unknown=True)
        return self.resume_map.get(sid, "skipped:not-found")


    async def get_session(self, sid: str):
        return SimpleNamespace(interrupted_by_restart=self.still_interrupted)


def _make_client() -> tuple[TestClient, _FakeSessions]:
    """Build a TestClient with a real orchestrator (for lifespan) and a fake sessions facade."""
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


@pytest.fixture
def client_and_fake():
    client, fake = _make_client()
    with patch.dict(os.environ, _ENV), client:
        yield client, fake


@pytest.fixture
def client(client_and_fake):
    return client_and_fake[0]


@pytest.fixture
def fake_orch(client_and_fake):
    return client_and_fake[1]


# ---------------------------------------------------------------------------
# Core contract tests (from task brief)
# ---------------------------------------------------------------------------


def test_batch_resume_partitions_by_outcome(client, fake_orch):
    """Mixed results: only "resumed" lands in succeeded; others go to failed."""
    fake_orch.resume_map = {"s1": "resumed", "s2": "skipped:not-interrupted"}
    resp = client.post(
        "/api/sessions/batch/resume",
        json={"ids": ["s1", "s2", "s1"]},
        headers=_H,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["succeeded"] == ["s1"]              # deduped, only resumed
    assert body["failed"] == [{"id": "s2", "error": "skipped:not-interrupted"}]


def test_batch_resume_reports_no_driver_as_failure(client, fake_orch):
    """no_driver result lands in failed."""
    fake_orch.resume_map = {"s1": "no_driver"}
    body = client.post(
        "/api/sessions/batch/resume",
        json={"ids": ["s1"]},
        headers=_H,
    ).json()
    assert body["succeeded"] == []
    assert body["failed"] == [{"id": "s1", "error": "no_driver"}]


# ---------------------------------------------------------------------------
# Auth / validation guards
# ---------------------------------------------------------------------------


def test_batch_resume_requires_identity():
    """No Tailscale-User-Login header → 401."""
    client, _ = _make_client()
    with patch.dict(os.environ, _ENV), client:
        resp = client.post(
            "/api/sessions/batch/resume",
            json={"ids": ["s1"]},
        )
    assert resp.status_code == 401


def test_batch_resume_empty_ids_returns_422():
    """ids must be non-empty — 422 on empty list."""
    client, _ = _make_client()
    with patch.dict(os.environ, _ENV), client:
        resp = client.post(
            "/api/sessions/batch/resume",
            json={"ids": []},
            headers=_H,
        )
    assert resp.status_code == 422


def test_batch_resume_oversized_ids_returns_422():
    """ids capped at 200 — 422 on 201."""
    client, _ = _make_client()
    ids = [f"x{i}" for i in range(201)]
    with patch.dict(os.environ, _ENV), client:
        resp = client.post(
            "/api/sessions/batch/resume",
            json={"ids": ids},
            headers=_H,
        )
    assert resp.status_code == 422


def test_batch_resume_all_skipped(client, fake_orch):
    """All ids produce skip results → succeeded empty, all in failed."""
    fake_orch.resume_map = {"a": "skipped:not-interrupted", "b": "skipped:not-found"}
    resp = client.post(
        "/api/sessions/batch/resume",
        json={"ids": ["a", "b"]},
        headers=_H,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["succeeded"] == []
    assert len(body["failed"]) == 2


def test_batch_resume_all_resumed(client, fake_orch):
    """All ids resumed → all in succeeded, failed empty."""
    fake_orch.resume_map = {"a": "resumed", "b": "resumed"}
    resp = client.post(
        "/api/sessions/batch/resume",
        json={"ids": ["a", "b"]},
        headers=_H,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["succeeded"]) == {"a", "b"}
    assert body["failed"] == []


def test_batch_resume_dedupes_ids(client, fake_orch):
    """Duplicate ids are deduplicated (each resumed once)."""
    fake_orch.resume_map = {"s1": "resumed"}
    resp = client.post(
        "/api/sessions/batch/resume",
        json={"ids": ["s1", "s1", "s1"]},
        headers=_H,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["succeeded"] == ["s1"]
    # Should only call resume_interrupted once for s1
    assert fake_orch.calls.count(("s1", "testuser@example.com")) == 1


def test_batch_resume_timeout_with_cleared_flag_is_success_and_not_a_warning(
        client, fake_orch, caplog):
    """#2818: edge timeout, daemon finished ⇒ succeeded, no auto-filing WARNING."""
    fake_orch.timeouts = {"s1"}
    with caplog.at_level("INFO", logger="server.api.sessions"):
        resp = client.post("/api/sessions/batch/resume", json={"ids": ["s1"]}, headers=_H)
    assert resp.json() == {"succeeded": ["s1"], "failed": []}
    assert not [r for r in caplog.records if r.levelname == "WARNING" and r.name == "server.api.sessions"]


def test_batch_resume_timeout_still_interrupted_is_pending_failure(
        client, fake_orch, caplog):
    fake_orch.timeouts = {"s1"}
    fake_orch.still_interrupted = True
    with caplog.at_level("INFO", logger="server.api.sessions"):
        resp = client.post("/api/sessions/batch/resume", json={"ids": ["s1"]}, headers=_H)
    body = resp.json()
    assert body["succeeded"] == []
    assert body["failed"][0]["id"] == "s1"
    assert not [r for r in caplog.records if r.levelname == "WARNING" and r.name == "server.api.sessions"]
