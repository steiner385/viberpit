"""Contract-shape + integration tests for structured-log breadcrumbs:
  - bg_task_rescan  (emitted by _snapshot_open_tasks inner _scan)
  - deploy.*        (emitted by DeployController.deploy_once)
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.obs.context import log_event


# ---------------------------------------------------------------------------
# Contract-shape tests: just verify the helper emits the right extra fields.
# These pass before any instrumentation is in place (the helper already works).
# ---------------------------------------------------------------------------


def test_bg_rescan_breadcrumb_shape(caplog):
    """log_event emits a record with event='bg_task_rescan' and int `open` field."""
    log = logging.getLogger("server.sessions.orchestrator")
    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        log_event(log, logging.INFO, "bg_task_rescan", session_id="s1", open=2)
    rec = [r for r in caplog.records if getattr(r, "event", None) == "bg_task_rescan"][-1]
    assert isinstance(rec.fields["open"], int)
    assert rec.fields["session_id"] == "s1"


def test_deploy_drain_breadcrumb_shape(caplog):
    """log_event emits a record with event='deploy.drain.start' and deploy_id field."""
    log = logging.getLogger("server.deploy")
    with caplog.at_level(logging.INFO, logger="server.deploy"):
        log_event(log, logging.INFO, "deploy.drain.start", deploy_id="dep-1")
    rec = [r for r in caplog.records if getattr(r, "event", None) == "deploy.drain.start"][-1]
    assert rec.fields["deploy_id"] == "dep-1"


def test_deploy_daemon_stop_breadcrumb_shape(caplog):
    log = logging.getLogger("server.deploy")
    with caplog.at_level(logging.INFO, logger="server.deploy"):
        log_event(log, logging.INFO, "deploy.daemon.stop", deploy_id="dep-2")
    rec = [r for r in caplog.records if getattr(r, "event", None) == "deploy.daemon.stop"][-1]
    assert rec.fields["deploy_id"] == "dep-2"


def test_deploy_sessions_reattach_breadcrumb_shape(caplog):
    log = logging.getLogger("server.deploy")
    with caplog.at_level(logging.INFO, logger="server.deploy"):
        log_event(log, logging.INFO, "deploy.sessions.reattach",
                  deploy_id="dep-3", recovered=2, lost=1)
    rec = [r for r in caplog.records
           if getattr(r, "event", None) == "deploy.sessions.reattach"][-1]
    assert rec.fields["deploy_id"] == "dep-3"
    assert isinstance(rec.fields["recovered"], int)
    assert isinstance(rec.fields["lost"], int)


# ---------------------------------------------------------------------------
# Integration test: drive _snapshot_open_tasks → assert bg_task_rescan fires.
# ---------------------------------------------------------------------------


class _MinimalOrch:
    """Minimal stand-in for SessionOrchestrator for _snapshot_open_tasks."""
    # Bind the real method so it runs against this instance.
    from server.sessions.orchestrator import SessionOrchestrator
    _snapshot_open_tasks = SessionOrchestrator._snapshot_open_tasks

    def __init__(self, drivers: dict, transcript_paths: dict):
        self._drivers = drivers
        self._transcript_paths = transcript_paths

    def transcript_path(self, sid: str):
        return self._transcript_paths.get(sid)


@asynccontextmanager
async def _fake_db_session():
    """Fake db_session that accepts execute calls without a real DB."""
    db = AsyncMock()
    db.execute = AsyncMock()
    yield db


def _write_transcript(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(o) for o in lines) + "\n")
    return str(p)


def _bg_bash_tool(tid):
    return {
        "type": "assistant", "uuid": "u",
        "message": {
            "model": "claude-opus-4-8",
            "content": [{
                "type": "tool_use", "id": tid, "name": "Bash",
                "input": {"command": "sleep 99", "run_in_background": True},
            }],
        },
    }


@pytest.mark.asyncio
async def test_bg_rescan_integration(tmp_path, caplog):
    """_snapshot_open_tasks emits bg_task_rescan with correct session_id and open count."""
    # Write a transcript with one open background task
    t_path = _write_transcript(tmp_path, "s1.jsonl", [_bg_bash_tool("toolu_1")])

    orch = _MinimalOrch(
        drivers={"s1": object()},
        transcript_paths={"s1": t_path},
    )

    with patch("server.sessions.orchestrator.db_session", side_effect=_fake_db_session), \
         caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        await orch._snapshot_open_tasks()

    rescan_recs = [r for r in caplog.records
                   if getattr(r, "event", None) == "bg_task_rescan"]
    assert len(rescan_recs) == 1
    rec = rescan_recs[0]
    assert rec.fields["session_id"] == "s1"
    assert isinstance(rec.fields["open"], int)
    assert rec.fields["open"] == 1


@pytest.mark.asyncio
async def test_bg_rescan_no_tasks_emits_zero(tmp_path, caplog):
    """_snapshot_open_tasks emits open=0 when transcript has no background tasks."""
    # Empty transcript
    t_path = _write_transcript(tmp_path, "s2.jsonl", [])

    orch = _MinimalOrch(
        drivers={"s2": object()},
        transcript_paths={"s2": t_path},
    )

    with patch("server.sessions.orchestrator.db_session", side_effect=_fake_db_session), \
         caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        await orch._snapshot_open_tasks()

    rescan_recs = [r for r in caplog.records
                   if getattr(r, "event", None) == "bg_task_rescan"]
    assert len(rescan_recs) == 1
    assert rescan_recs[0].fields["open"] == 0
    assert rescan_recs[0].fields["session_id"] == "s2"


# ---------------------------------------------------------------------------
# Deploy integration: drive deploy_once in a controlled harness and assert
# deploy breadcrumbs are emitted.
# ---------------------------------------------------------------------------


def _make_deploy_controller(*, daemon_enabled=True, daemon_changed=True,
                             drain_result="ready", restore_result=None):
    """Build a DeployController with all external calls mocked."""
    from server.deploy.controller import DeployController

    git_mod = MagicMock()
    git_mod.fetch.return_value = None
    git_mod.current_sha.return_value = "aaa"
    git_mod.remote_sha.return_value = "bbb"
    git_mod.short_sha.return_value = "bbb123"

    steps_mod = MagicMock()
    steps_mod.ci_verdict.return_value = "green"
    steps_mod.guard_working_tree.return_value = False
    steps_mod.fast_forward.return_value = None
    steps_mod.sync_deps_if_changed.return_value = []
    steps_mod.build_frontend.return_value = None
    steps_mod.classify_change.return_value = (
        "daemon_changed" if daemon_changed else "edge_only"
    )
    steps_mod.restart_units.return_value = []
    steps_mod.wait_healthy.return_value = True
    steps_mod.publish_deployment.return_value = None

    drain_mod = MagicMock()
    drain_mod.drain_and_wait = AsyncMock(return_value=drain_result)

    restore_resp = restore_result or {"restored": ["s1"], "failed": []}
    client = AsyncMock()
    client.call = AsyncMock(side_effect=lambda method, **kw: (
        None if method == "snapshot_deploy" else restore_resp
    ))

    dc = DeployController(
        repo="/fake/repo",
        health_url="http://localhost/health",
        venv_python="/fake/python",
        daemon_client_factory=lambda: client,
        daemon_enabled=daemon_enabled,
        steps_mod=steps_mod,
        git_mod=git_mod,
        drain_mod=drain_mod,
        changed_files_fn=lambda r, a, b: ["server/sessions/orchestrator.py"],
    )
    return dc


@pytest.mark.asyncio
async def test_deploy_drain_start_breadcrumb(caplog):
    """deploy_once emits deploy.drain.start when daemon_changed=True."""
    dc = _make_deploy_controller(daemon_enabled=True, daemon_changed=True)

    with caplog.at_level(logging.INFO, logger="server.deploy"):
        await dc.deploy_once()

    drain_recs = [r for r in caplog.records
                  if getattr(r, "event", None) == "deploy.drain.start"]
    assert len(drain_recs) == 1
    assert drain_recs[0].fields["deploy_id"] is not None


@pytest.mark.asyncio
async def test_deploy_daemon_stop_start_breadcrumbs(caplog):
    """deploy_once emits daemon stop + start breadcrumbs when daemon restarts."""
    dc = _make_deploy_controller(daemon_enabled=True, daemon_changed=True)

    with caplog.at_level(logging.INFO, logger="server.deploy"):
        await dc.deploy_once()

    stop_recs = [r for r in caplog.records
                 if getattr(r, "event", None) == "deploy.daemon.stop"]
    start_recs = [r for r in caplog.records
                  if getattr(r, "event", None) == "deploy.daemon.start"]
    assert len(stop_recs) == 1
    assert len(start_recs) == 1
    # All share the same deploy_id
    assert stop_recs[0].fields["deploy_id"] == start_recs[0].fields["deploy_id"]


@pytest.mark.asyncio
async def test_deploy_edge_cutover_breadcrumb(caplog):
    """deploy_once always emits deploy.edge.cutover after health check passes."""
    dc = _make_deploy_controller(daemon_enabled=True, daemon_changed=True)

    with caplog.at_level(logging.INFO, logger="server.deploy"):
        await dc.deploy_once()

    cutover_recs = [r for r in caplog.records
                    if getattr(r, "event", None) == "deploy.edge.cutover"]
    assert len(cutover_recs) == 1


@pytest.mark.asyncio
async def test_deploy_sessions_reattach_breadcrumb(caplog):
    """deploy_once emits deploy.sessions.reattach with recovered/lost counts."""
    dc = _make_deploy_controller(
        daemon_enabled=True, daemon_changed=True,
        restore_result={"restored": ["s1", "s2"], "failed": ["s3"]},
    )

    with caplog.at_level(logging.INFO, logger="server.deploy"):
        await dc.deploy_once()

    reattach_recs = [r for r in caplog.records
                     if getattr(r, "event", None) == "deploy.sessions.reattach"]
    assert len(reattach_recs) == 1
    rec = reattach_recs[0]
    assert rec.fields["recovered"] == 2
    assert rec.fields["lost"] == 1
    # The full RPC result must land in the controller's journal: the status
    # object is wiped by the controller's own scheduled self-restart (issue #864).
    assert rec.fields["restored"] == ["s1", "s2"]
    assert rec.fields["failed"] == ["s3"]


@pytest.mark.asyncio
async def test_deploy_restore_rpc_failure_is_logged(caplog):
    """A failed restore_deploy RPC must leave a durable log line, not just a
    status warning that the controller's self-restart wipes (issue #864)."""
    dc = _make_deploy_controller(daemon_enabled=True, daemon_changed=True)

    async def _call(method, **kw):
        if method == "restore_deploy":
            raise RuntimeError("daemon went away")
        return None

    # Rewire the shared mock client the factory hands out.
    dc._client_factory().call = AsyncMock(side_effect=_call)

    with caplog.at_level(logging.INFO, logger="server.deploy"):
        st = await dc.deploy_once()

    assert any("session restore call failed" in w for w in st.warnings)
    assert any("restore_deploy" in r.getMessage() for r in caplog.records
               if r.levelno >= logging.WARNING)


@pytest.mark.asyncio
async def test_deploy_no_drain_when_edge_only(caplog):
    """deploy_once does NOT emit drain/daemon breadcrumbs when change is edge_only."""
    dc = _make_deploy_controller(daemon_enabled=True, daemon_changed=False)

    with caplog.at_level(logging.INFO, logger="server.deploy"):
        await dc.deploy_once()

    drain_recs = [r for r in caplog.records
                  if getattr(r, "event", None) == "deploy.drain.start"]
    assert len(drain_recs) == 0
    # edge.cutover should still fire
    cutover_recs = [r for r in caplog.records
                    if getattr(r, "event", None) == "deploy.edge.cutover"]
    assert len(cutover_recs) == 1


@pytest.mark.asyncio
async def test_deploy_id_consistent_across_breadcrumbs(caplog):
    """All breadcrumbs within one deploy_once share the same deploy_id."""
    dc = _make_deploy_controller(daemon_enabled=True, daemon_changed=True)

    with caplog.at_level(logging.INFO, logger="server.deploy"):
        await dc.deploy_once()

    deploy_recs = [r for r in caplog.records
                   if getattr(r, "event", "").startswith("deploy.") and getattr(r, "fields", None)]
    assert len(deploy_recs) >= 4  # drain.start, daemon.stop, daemon.start, edge.cutover, reattach
    ids = {r.fields["deploy_id"] for r in deploy_recs}
    assert len(ids) == 1, f"Expected one deploy_id across all breadcrumbs, got: {ids}"
