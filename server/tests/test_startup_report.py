"""Startup health report (2026-08-29 startup-health incident).

A failed ``rehydrate()`` used to be swallowed into a log line ("starting with no
sessions restored") — the daemon looked healthy while every session was gone.
The orchestrator now records a structured startup report (rehydrated/rearmed
counts + any boot-step error) that ``/api/daemon/health`` exposes, so a bad boot
is observable instead of silent. The deploy's functional probes also gain
``/api/github/repos``: the endpoint whose failure made the New Session dropdown
useless (see test_repos.py's daemon_down tests) is now deploy-gated.
"""
import asyncio
import os
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

REPO_ROOT = Path(__file__).resolve().parents[2]
_IDENT = {"Tailscale-User-Login": "startup@example.com"}


def test_startup_report_records_rehydrated_count(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'report.db'}"

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="a", name="A", account_name="acct",
                             engine_session_id="c", status="idle", cwd=str(tmp_path)))
            db.add(DbSession(id="b", name="B", account_name="acct",
                             engine_session_id="c", status="working", cwd=str(tmp_path)))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        report = orch.startup_report()
        await orch.stop()
        return report

    report = asyncio.run(run())
    assert report["rehydrated"] == 2
    assert report["rehydrateError"] is None
    assert report["startedAt"]  # ISO stamp present


def test_startup_report_captures_boot_step_failure():
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    orch.note_startup_failure("rehydrate", RuntimeError("db exploded"))
    report = orch.startup_report()
    assert "RuntimeError" in report["rehydrateError"]
    assert "db exploded" in report["rehydrateError"]
    assert report["rehydrated"] is None


def test_daemon_health_exposes_startup_report(tmp_path):
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
        app = create_app(orchestrator=orch)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/daemon/health", headers=_IDENT)
    assert resp.status_code == 200
    startup = resp.json().get("startup")
    assert startup is not None
    assert "rehydrated" in startup
    assert "rehydrateError" in startup


def test_deploy_probes_include_github_repos():
    """The endpoint whose silent failure blanked the New Session dropdown must be
    part of the deploy's functional gate, not just /healthz."""
    from server.deploy.steps import DEFAULT_PROBE_PATHS
    assert "/api/github/repos" in DEFAULT_PROBE_PATHS


def test_startup_report_rpc_is_registered():
    """Remote mode: the edge reads the report over the daemon RPC bridge."""
    text = (REPO_ROOT / "server" / "ipc" / "orchestrator_rpc.py").read_text()
    assert '"startup_report"' in text, "startup_report missing from the RPC allowlist"


def test_lifespans_record_rehydrate_failure_in_report():
    """Both boot paths (in-process app.py and daemon serve.py) must feed a
    rehydrate() failure into the report instead of only logging it."""
    for rel in ("server/app.py", "server/ipc/serve.py"):
        text = (REPO_ROOT / rel).read_text()
        assert "note_startup_failure" in text, (
            f"{rel} swallows rehydrate() failures without recording them")
