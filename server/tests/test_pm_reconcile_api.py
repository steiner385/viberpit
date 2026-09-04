"""PM reconcile-journal API tests — GET /api/pm/reconcile/journal.

Mirrors test_pm_deps_api.py fixture pattern: _client() context manager, async ORM
seeding via db_session(), CCSWITCH_DEV=1 + empty allowlist auth bypass.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client():
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    _clear = [
        "CCSWITCH_SESSIOND_ENDPOINT",
        "CCSWITCH_SESSIOND_SOCKET",
        "CCSWITCH_SESSIOND_HOST",
        "CCSWITCH_SESSIOND_PORT",
        "CCSWITCH_SESSIOND_CLIENT_CERT",
        "CCSWITCH_SESSIOND_CLIENT_KEY",
        "CCSWITCH_SESSIOND_CA",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        saved = {k: os.environ.pop(k, None) for k in _clear}
        try:
            with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url,
                                         "CCSWITCH_DEV": "1",
                                         "CCSWITCH_TAILNET_ALLOWLIST": ""}):
                orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
                app = create_app(orchestrator=orch)
                with TestClient(app, raise_server_exceptions=False) as c:
                    yield c
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


def _seed_rows() -> None:
    from datetime import datetime, timezone

    from server.db import db_session
    from server.pm import journal
    from server.pm.reconcile_diff import Mutation

    async def _seed_async() -> None:
        async with db_session() as db:
            now = datetime.now(timezone.utc)

            def _seed_sync(s) -> None:
                dry_run_ids = journal.record_pending(
                    s, [Mutation(kind="set_field", target_node_id="n1", field="Status",
                                old_value=None, new_value="Ready", initiated_by="reconciler")],
                    batch_id="b1", now=now, dry_run=True)
                applied_ids = journal.record_pending(
                    s, [Mutation(kind="set_field", target_node_id="n2", field="Status",
                                old_value=None, new_value="Backlog", initiated_by="reconciler")],
                    batch_id="b2", now=now, dry_run=False)
                journal.mark_applied(s, applied_ids, now)
                parked_ids = journal.record_pending(
                    s, [Mutation(kind="set_field", target_node_id="n3", field="Status",
                                old_value=None, new_value="Needs human", initiated_by="reconciler")],
                    batch_id="b3", now=now, dry_run=False)
                journal.park(s, parked_ids[0], "GraphQL error", now)
                _ = dry_run_ids

            await db.run_sync(_seed_sync)
            await db.commit()

    asyncio.run(_seed_async())


def test_journal_endpoint_lists_and_counts():
    with _client() as c:
        _seed_rows()

        resp = c.get("/api/pm/reconcile/journal?state=parked", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [r["state"] for r in body["rows"]] == ["parked"]
        assert body["counts"] == {
            "pending": 0, "applied": 1, "failed": 0, "parked": 1, "dry_run": 1}

        row = body["rows"][0]
        assert row["targetNodeId"] == "n3"
        assert row["field"] == "Status"
        assert row["newValue"] == "Needs human"
        assert row["error"] == "parked: GraphQL error"
        assert row["batchId"] == "b3"
        assert row["initiatedBy"] == "reconciler"
        assert row["createdAt"] is not None
        assert row["updatedAt"] is not None

        assert c.get(
            "/api/pm/reconcile/journal?state=bogus", headers=IDENTITY_HEADERS
        ).status_code == 400


def test_journal_endpoint_no_state_filter_returns_all():
    with _client() as c:
        _seed_rows()

        resp = c.get("/api/pm/reconcile/journal", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["rows"]) == 3
        assert body["counts"] == {
            "pending": 0, "applied": 1, "failed": 0, "parked": 1, "dry_run": 1}


def test_journal_endpoint_requires_identity():
    """The journal is the full record of what ccswitch wrote to GitHub — node ids, field
    values, batch ids — so it is behind the same identity gate as every other PM route.
    Asserted explicitly because the endpoint takes no path parameter and reads a table
    no other test route touches: a missing Depends here would fail silently."""
    with _client() as c:
        _seed_rows()
        assert c.get("/api/pm/reconcile/journal").status_code == 401
        assert c.get("/api/pm/reconcile/journal?state=applied").status_code == 401
        # ... and the same request WITH an identity succeeds, so 401 is the gate
        # answering, not a route that is simply broken.
        assert c.get("/api/pm/reconcile/journal",
                     headers=IDENTITY_HEADERS).status_code == 200


def test_journal_endpoint_limit_clamped():
    with _client() as c:
        _seed_rows()

        resp = c.get("/api/pm/reconcile/journal?limit=10000", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        # 3 seeded rows, well under the 1000 clamp — just confirm it doesn't 500/422.
        assert len(resp.json()["rows"]) == 3
