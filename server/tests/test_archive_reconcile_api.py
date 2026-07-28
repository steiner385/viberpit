"""Archive/unarchive reconcile when a daemon RPC response is lost.

Root cause (2026-07-28): the archive (DELETE /api/sessions/{id}) and unarchive
(POST /api/sessions/{id}/unarchive) handlers RPC to the daemon. When the daemon is
briefly wedged or restarting, the RPC times out / the connection resets, surfacing as
``DaemonUnavailable`` -> HTTP 503. But the daemon frequently *completes the operation
anyway* — only the response is lost. The frontend then shows a spurious red error
toast even though the op worked ("error toast but it works anyway").

Fix: on ``DaemonUnavailable`` the handler reconciles by re-checking actual state and
returns success if the op took effect; a genuine failure (still live / still archived,
or the daemon truly unreachable) still surfaces the 503.

These tests use the in-process SessionOrchestrator and wrap its archive_session /
unarchive to simulate "the op succeeded but the RPC response was lost".
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from ccswitch.selector import Picked
from tests.helpers import temp_env, usage as make_usage
from server.ipc.client import DaemonUnavailable
from server.tests.fakes.fake_factory import fake_sdk_factory

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


def _make_usage_fn(accounts: dict):
    def usage_fn(name: str):
        return accounts.get(name)
    return usage_fn


def _seed_slot(name: str) -> None:
    import time as _time
    from ccswitch import paths
    store = paths.store_dir()
    store.mkdir(parents=True, exist_ok=True)
    now_ms = int(_time.time() * 1000)
    (store / f"{name}.cred.json").write_text(
        f'{{"accessToken":"{name}-tok","refreshToken":"{name}-ref",'
        f'"expiresAt":{now_ms + 9_000_000},"subscriptionType":"max"}}'
    )


@contextmanager
def _app_with_orch(d):
    """Yield (client, orch) with one seeded account 'account1', inside lifespan."""
    _seed_slot("account1")
    usage_fn = _make_usage_fn({"account1": make_usage(session=10.0, weekly=20.0)})
    with patch("ccswitch.accounts.usage_for", side_effect=usage_fn), \
         patch("server.sessions.orchestrator.select_account", return_value=Picked("account1")), \
         patch("server.sessions.orchestrator.compose", return_value=d / "cfg" / "account1"), \
         patch.dict(os.environ, {"CCSWITCH_DB_URL": f"sqlite+aiosqlite:///{d / 'ccswitch.db'}"}):
        (d / "cfg" / "account1").mkdir(parents=True, exist_ok=True)
        from server.sessions.orchestrator import SessionOrchestrator
        from server.app import create_app
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), usage_fn=usage_fn, poll_interval=9999.0)
        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            yield client, orch


def _create(client, name):
    r = client.post("/api/sessions", json={"name": name, "account": "account1"}, headers=IDENTITY_HEADERS)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


def test_archive_returns_204_when_op_succeeds_but_response_lost():
    """DaemonUnavailable AFTER the archive took effect reconciles to 204, not 503."""
    with temp_env() as d:
        with _app_with_orch(d) as (client, orch):
            sid = _create(client, "Ghosted")
            real = orch.archive_session

            async def archive_then_lose_response(session_id):
                await real(session_id)                 # op actually completes
                raise DaemonUnavailable("archive_session timed out: response lost")

            with patch.object(orch, "archive_session", side_effect=archive_then_lose_response):
                resp = client.delete(f"/api/sessions/{sid}", headers=IDENTITY_HEADERS)

            assert resp.status_code == 204, resp.text
            # And it really is archived.
            arch = client.get("/api/sessions/archived", headers=IDENTITY_HEADERS).json()
            assert any(a["id"] == sid for a in arch)


def test_archive_returns_503_when_op_did_not_take_effect():
    """DaemonUnavailable with NO state change stays a 503 (genuine failure)."""
    with temp_env() as d:
        with _app_with_orch(d) as (client, orch):
            sid = _create(client, "StillHere")

            async def archive_fails(session_id):
                raise DaemonUnavailable("connection reset")

            with patch.object(orch, "archive_session", side_effect=archive_fails):
                resp = client.delete(f"/api/sessions/{sid}", headers=IDENTITY_HEADERS)

            assert resp.status_code == 503, resp.text
            # Session is untouched — still live.
            live = client.get("/api/sessions", headers=IDENTITY_HEADERS).json()
            assert any(s["id"] == sid for s in live)


# ---------------------------------------------------------------------------
# Unarchive
# ---------------------------------------------------------------------------


def test_unarchive_returns_200_when_op_succeeds_but_response_lost():
    """DaemonUnavailable AFTER unarchive took effect reconciles to 200 with the record."""
    with temp_env() as d:
        with _app_with_orch(d) as (client, orch):
            sid = _create(client, "Comeback")
            assert client.delete(f"/api/sessions/{sid}", headers=IDENTITY_HEADERS).status_code == 204
            real = orch.unarchive

            async def unarchive_then_lose_response(session_id):
                rec = await real(session_id)           # op actually completes
                assert rec is not None
                raise DaemonUnavailable("unarchive timed out: response lost")

            with patch.object(orch, "unarchive", side_effect=unarchive_then_lose_response):
                resp = client.post(f"/api/sessions/{sid}/unarchive", headers=IDENTITY_HEADERS)

            assert resp.status_code == 200, resp.text
            assert resp.json()["id"] == sid
            # It reappears as a live session.
            live = client.get("/api/sessions", headers=IDENTITY_HEADERS).json()
            assert any(s["id"] == sid for s in live)


def test_unarchive_returns_503_when_op_did_not_take_effect():
    """DaemonUnavailable with NO state change stays a 503 (genuine failure)."""
    with temp_env() as d:
        with _app_with_orch(d) as (client, orch):
            sid = _create(client, "Stuck")
            assert client.delete(f"/api/sessions/{sid}", headers=IDENTITY_HEADERS).status_code == 204

            async def unarchive_fails(session_id):
                raise DaemonUnavailable("connection reset")

            with patch.object(orch, "unarchive", side_effect=unarchive_fails):
                resp = client.post(f"/api/sessions/{sid}/unarchive", headers=IDENTITY_HEADERS)

            assert resp.status_code == 503, resp.text
            # Still archived, not live.
            arch = client.get("/api/sessions/archived", headers=IDENTITY_HEADERS).json()
            assert any(a["id"] == sid for a in arch)
