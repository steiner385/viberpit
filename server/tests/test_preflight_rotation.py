"""FR-304 outside the queue drain: a DIRECT launch (create_session) whose auto-picked
account fails credential preflight is retried once on a rotated account; a pinned
account or a second failure surfaces as the typed ``PreflightFailed`` — a 409 at the
API, an INFO (not an auto-filing WARNING) at the daemon RPC boundary, and a faithful
reconstruction on the edge in remote mode (#947)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from server.sessions import provenance
from server.sessions.preflight import PreflightFailed
from server.sessions.selection import Picked
from server.tests.test_session_queue_drain import _make_orch, _seed_account, _stop_orch


def _write_creds(cfg_dir, *, expired: bool) -> None:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    blob = {"accessToken": "tok", "refreshToken": "ref",
            "expiresAt": int(time.time() * 1000) + 3_600_000}
    if expired:
        blob = {"accessToken": "tok", "expiresAt": 1}       # lapsed, nothing to refresh
    (cfg_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": blob}))


def _patches(tmp_path, excludes: list[frozenset]):
    def picking(exclude=frozenset(), **kw):
        excludes.append(frozenset(exclude))
        return Picked("beta" if "alpha" in exclude else "alpha")
    return (patch("server.sessions.orchestrator.select_account", side_effect=picking),
            patch("server.sessions.orchestrator.compose",
                  side_effect=lambda account: tmp_path / "cfg" / account))


def test_direct_create_rotates_once_when_the_picked_account_fails_preflight(tmp_path):
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'direct.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=3)
        try:
            _seed_account("beta")
            _write_creds(tmp_path / "cfg" / "alpha", expired=True)
            _write_creds(tmp_path / "cfg" / "beta", expired=False)
            excludes: list[frozenset] = []
            sel, comp = _patches(tmp_path, excludes)
            with sel, comp:
                rec = await orch.create_session("direct")
            events = await provenance.list_events(rec.id)
            return rec, excludes, events
        finally:
            await _stop_orch(orch)

    rec, excludes, events = asyncio.run(run())
    assert rec.status == "idle" and rec.account == "beta"
    assert excludes[0] == frozenset() and "alpha" in excludes[-1]
    assert [e["kind"] for e in events] == [provenance.ROTATED_ACCOUNT]
    assert events[0]["detail"] == {"from": "alpha", "reason": "credentials_expired",
                                   "attempt": 1}


def test_direct_create_raises_typed_when_the_rotated_account_fails_too(tmp_path):
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'both.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=3)
        try:
            _seed_account("beta")
            _write_creds(tmp_path / "cfg" / "alpha", expired=True)
            _write_creds(tmp_path / "cfg" / "beta", expired=True)
            excludes: list[frozenset] = []
            sel, comp = _patches(tmp_path, excludes)
            with sel, comp, pytest.raises(PreflightFailed) as raised:
                await orch.create_session("direct")
            return raised.value, excludes, dict(orch._records)
        finally:
            await _stop_orch(orch)

    exc, excludes, records = asyncio.run(run())
    assert (exc.account, exc.reason) == ("beta", "credentials_expired")
    assert len(excludes) == 2, "exactly one rotation, never a loop"
    assert not any(r.name == "direct" for r in records.values()), "nothing was started"


def test_pinned_account_failing_preflight_is_not_rotated(tmp_path):
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'pinned.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=3)
        try:
            _write_creds(tmp_path / "cfg" / "alpha", expired=True)
            excludes: list[frozenset] = []
            sel, comp = _patches(tmp_path, excludes)
            with sel, comp, pytest.raises(PreflightFailed) as raised:
                await orch.create_session("direct", account="alpha")
            return raised.value, excludes
        finally:
            await _stop_orch(orch)

    exc, excludes = asyncio.run(run())
    assert (exc.account, exc.reason) == ("alpha", "credentials_expired")
    assert excludes == [], "a pinned account is never re-picked"


# --- API / RPC surfaces -------------------------------------------------------------

def test_post_sessions_maps_preflight_failure_to_a_typed_409(isolated_db):
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    from server.tests.test_api import IDENTITY_HEADERS

    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
    orch.create_session = AsyncMock(  # type: ignore[method-assign]
        side_effect=PreflightFailed("alpha", "credentials_expired"))
    app = create_app(orchestrator=orch)
    with TestClient(app, raise_server_exceptions=False) as client:
        # No pinned account in the body: the route validates one against the manifest
        # before the orchestrator runs; the (mocked) spawn is what fails here.
        response = client.post("/api/sessions", json={"name": "Stale"},
                               headers=IDENTITY_HEADERS)
    assert response.status_code == 409, response.text
    assert response.json()["error"] == {
        "code": "credential_preflight_failed", "reason": "credentials_expired",
        "account": "alpha", "message": str(PreflightFailed("alpha", "credentials_expired"))}


def test_daemon_logs_preflight_refusal_at_info(caplog):
    from server.ipc.daemon import create_daemon_app, register_rpc

    app = create_daemon_app()

    async def stale(params):
        raise PreflightFailed("alpha", "missing_credentials")

    register_rpc(app, "stale", stale)
    with caplog.at_level(logging.INFO, logger="server.ipc.daemon"), TestClient(app) as c:
        body = c.post("/rpc/stale", json={}).json()
    recs = [r for r in caplog.records if "rpc.error method=stale" in r.getMessage()]
    assert recs and all(r.levelno == logging.INFO for r in recs)
    assert body["error"]["type"] == "PreflightFailed"
    assert body["error"]["data"] == {"account": "alpha", "reason": "missing_credentials"}


def test_edge_rebuilds_preflight_failure_from_the_envelope():
    from server.ipc.client import DaemonError, reconstruct_create_session_error

    exc = reconstruct_create_session_error(DaemonError(
        "PreflightFailed", "x", {"account": "alpha", "reason": "credentials_expired"}))
    assert isinstance(exc, PreflightFailed)
    assert (exc.account, exc.reason) == ("alpha", "credentials_expired")
