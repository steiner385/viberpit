"""Fleet-global side effects riding per-host pollers are primary-only (#2273).

Sibling sweep of #2106. ``_PRIMARY_ONLY_LOOP_ATTRS`` (pinned by
``test_primary_lease``) only sees automations that own a loop task; these two
hang off per-host machinery instead, so the task-attribute gate structurally
cannot see them. These tests pin the BEHAVIOUR:

- the merge-queue sink wired into ``PrPopulationPoller`` writes no
  ``MergeQueueObservation`` rows on a worker (``record()`` is a plain INSERT, so
  a worker would interleave a second observer's snapshots into the shared series)
  while the poller itself keeps running there;
- ``DeployWatcher`` never starts on a worker (duplicate health polls + GitHub
  compare spend; its only reader, ``deploy_status()``, is routed to the primary).
"""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from server.db import db_session, init_db
from server.db.models import MergeQueueObservation
from server.github.merge_queue import QueueEntry
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


def _new_orch():
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)


def _entry() -> QueueEntry:
    return QueueEntry(pr_number=7, position=1, state="QUEUED", enqueued_at=None,
                      batch_head_oid=None, group_checks=None, checks_total=None,
                      checks_done=None, eta_seconds=None)


async def _observation_count() -> int:
    async with db_session() as s:
        return int(await s.scalar(select(func.count()).select_from(MergeQueueObservation)) or 0)


def _run_sink(tmp_path) -> int:
    async def run() -> int:
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'mq.db'}")
        orch = _new_orch()
        sink = orch._pr_population._merge_queue_sink
        assert sink is not None, "the poller must still carry the sink on every role"
        await sink({"o/r": (_entry(),)}, 1_700_000_000)
        return await _observation_count()

    return asyncio.run(run())


def test_worker_merge_queue_sink_writes_no_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "worker")
    assert _run_sink(tmp_path) == 0


def test_primary_merge_queue_sink_still_records(monkeypatch, tmp_path):
    """The mirror: the gate must not disable the merge-queue series outright."""
    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)
    assert _run_sink(tmp_path) == 1


def _started_tasks(tmp_path) -> set[str]:
    async def run() -> set[str]:
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'dw.db'}")
        orch = _new_orch()
        await orch.start()
        try:
            names = {t.get_name() for t in asyncio.all_tasks()}
            assert orch._pr_population._task is not None, \
                "the population poller must keep running (pr_for() fallback)"
            return names
        finally:
            await orch.stop()

    return asyncio.run(run())


def test_worker_never_starts_the_deploy_watcher(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "worker")
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "rocky")
    assert "deploy-watcher" not in _started_tasks(tmp_path)


def test_primary_still_starts_the_deploy_watcher(monkeypatch, tmp_path):
    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "dobby")
    assert "deploy-watcher" in _started_tasks(tmp_path)
