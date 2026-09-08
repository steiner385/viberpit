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
from server.sessions.engine import AgentEngine


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


def test_deploy_edge_cutover_breadcrumb_shape(caplog):
    """log_event emits a record with event='deploy.edge.cutover' and deploy_id field.

    The drain/daemon-stop/daemon-start breadcrumbs are gone from the controller: draining
    and restarting a daemon is per-runner work, done (and logged) inside
    ``server.deploy.runners.RunnerDeploy``.
    """
    log = logging.getLogger("server.deploy")
    with caplog.at_level(logging.INFO, logger="server.deploy"):
        log_event(log, logging.INFO, "deploy.edge.cutover", deploy_id="dep-1")
    rec = [r for r in caplog.records if getattr(r, "event", None) == "deploy.edge.cutover"][-1]
    assert rec.fields["deploy_id"] == "dep-1"


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
    """Minimal stand-in for SessionOrchestrator for _snapshot_open_tasks.

    ``_open_background_work`` is bound too (not stubbed): it is the per-engine seam
    the snapshot reads through since #1284, so a fake would let the breadcrumb pass
    while the real dispatch was broken.
    """
    # Bind the real methods so they run against this instance.
    from server.sessions.orchestrator import SessionOrchestrator
    _snapshot_open_tasks = SessionOrchestrator._snapshot_open_tasks
    _open_background_work = SessionOrchestrator._open_background_work

    def __init__(self, drivers: dict, transcript_paths: dict,
                 engine=AgentEngine.CLAUDE_CODE):
        self._drivers = drivers
        self._transcript_paths = transcript_paths
        self._engine = engine

    def transcript_path(self, sid: str):
        return self._transcript_paths.get(sid)

    def _engine_of(self, sid: str):
        return self._engine


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
                             restore_result=None):
    """Build a DeployController with all external calls mocked.

    The per-daemon RPCs (snapshot, drain, close_events, restart, restore) moved into
    ``server.deploy.runners``: the controller now fans out through ``runners_mod`` and
    reads each runner's restore result off the returned HostProgress, so this harness
    fakes that module instead of a DaemonClient factory.
    """
    from server.deploy.controller import DeployController
    from server.deploy.runners import HostProgress, RunnerTarget

    git_mod = MagicMock()
    git_mod.fetch.return_value = None
    git_mod.current_sha.return_value = "aaa"
    git_mod.remote_sha.return_value = "bbb"
    git_mod.short_sha.return_value = "bbb123"

    steps_mod = MagicMock()
    steps_mod.ci_verdict.return_value = "green"
    # Must be an explicit empty list: a bare MagicMock return is TRUTHY, which preflight
    # reads as "these units cannot be restarted" and refuses the deploy.
    steps_mod.check_unit_interpreters.return_value = []
    steps_mod.guard_working_tree.return_value = False
    steps_mod.fast_forward.return_value = None
    steps_mod.sync_deps_if_changed.return_value = []
    steps_mod.build_frontend.return_value = None
    steps_mod.classify_change.return_value = (
        "daemon_changed" if daemon_changed else "edge_only"
    )
    steps_mod.deploy_code_changed.return_value = False
    steps_mod.restart_edge.return_value = ["ccswitch.service"]
    steps_mod.remote_runners_configured.return_value = False
    steps_mod.wait_healthy.return_value = True
    steps_mod.publish_deployment.return_value = None

    restore_resp = restore_result if restore_result is not None else {"restored": ["s1"],
                                                                     "failed": []}
    target = RunnerTarget(name="rocky", endpoint="https://rocky:9443", enabled=True,
                          healthy=True, ssh_host="rocky", repo_path="/srv/cc")
    runners_mod = MagicMock()
    runners_mod.FAILED_STATES = frozenset({"failed", "unconfigured"})
    runners_mod.load_runner_targets = AsyncMock(return_value=[target])
    runners_mod.deploy_runners = AsyncMock(return_value={
        "rocky": HostProgress(state="done", sha="bbb123", moved=True,
                              restore=restore_resp if daemon_changed else None)})
    runners_mod.rewind_runners = AsyncMock(return_value=None)

    state_mod = MagicMock()
    state_mod.record_deploy = AsyncMock(return_value=None)

    dc = DeployController(
        repo="/fake/repo",
        health_url="http://localhost/health",
        venv_python="/fake/python",
        daemon_enabled=daemon_enabled,
        steps_mod=steps_mod,
        git_mod=git_mod,
        runners_mod=runners_mod,
        state_mod=state_mod,
        local_name="dobby",
        changed_files_fn=lambda r, a, b: ["server/sessions/orchestrator.py"],
    )
    return dc


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
    """The RESUME phase folds every runner's restore into one breadcrumb with
    recovered/lost counts."""
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
    # The full outcome must land in the controller's journal: the status object is
    # wiped by the controller's own scheduled self-restart (issue #864).
    assert rec.fields["restored"] == ["s1", "s2"]
    assert rec.fields["failed"] == ["s3"]


@pytest.mark.asyncio
async def test_deploy_edge_only_reattaches_nothing_but_still_cuts_over(caplog):
    """An edge-only deploy restarts no daemon, so no runner reports a restore — the
    reattach breadcrumb reports zero and edge.cutover still fires."""
    dc = _make_deploy_controller(daemon_enabled=True, daemon_changed=False)

    with caplog.at_level(logging.INFO, logger="server.deploy"):
        await dc.deploy_once()

    reattach_recs = [r for r in caplog.records
                     if getattr(r, "event", None) == "deploy.sessions.reattach"]
    assert len(reattach_recs) == 1
    assert reattach_recs[0].fields["recovered"] == 0
    assert reattach_recs[0].fields["lost"] == 0
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
    assert len(deploy_recs) >= 2  # edge.cutover, sessions.reattach
    ids = {r.fields["deploy_id"] for r in deploy_recs}
    assert len(ids) == 1, f"Expected one deploy_id across all breadcrumbs, got: {ids}"
