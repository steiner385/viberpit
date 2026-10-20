"""Orchestrator LAUNCH gate — hold a gated-model session pending approval.

`create_session` is the single chokepoint for BOTH the API and automations, so the
model-approval gate lives here. With the ops defaults (denylist=``fable``,
action=``require_approval``) a ``fable`` session is PARKED — persisted
``pending_approval`` with the requested model in ``pending_model``, no driver, no
capacity slot — while ``opus`` spawns normally and a ``block`` action is refused.

Harness mirrors test_orchestrator_origin.py: a fake SDK factory, a seeded account,
select_account patched, an in-memory SQLite DB via init_db.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from sqlalchemy import select

from viberpit.selector import Picked
from server.db import init_db, db_session
from server.sessions.orchestrator import SessionOrchestrator, ModelBlockedError
from server.tests.fakes.fake_factory import fake_sdk_factory


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from viberpit.quota import anthropic_snapshot
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return anthropic_snapshot(10.0, reset, 10.0, reset, False)


def _seed_account(name: str) -> None:
    import time as _time
    from viberpit import paths
    from viberpit.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok", "refreshToken": f"{name}-ref",
        "expiresAt": int(_time.time() * 1000) + 9_000_000, "subscriptionType": "max",
    })
    if not paths.shared_mcp_path().exists():
        write_json_secure(paths.shared_mcp_path(), {})


def _harness(tmp_path):
    """Build the env/patch context + an async runner factory for one orchestrator run."""
    store_home = tmp_path / "store"; store_home.mkdir()
    shared_base = tmp_path / "dot-claude"; shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"; cfg_dir.mkdir(parents=True)
    repo_root = tmp_path / "reporoot"; repo_root.mkdir()
    db_path = tmp_path / "gate_test.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"

    env_patch = patch.dict(os.environ, {
        "VIBERPIT_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "VIBERPIT_DB_URL": db_url,
    })
    compose_patch = patch("server.sessions.orchestrator.compose", return_value=cfg_dir)
    trust_patch = patch("server.sessions.orchestrator.configdir.trust_project")
    select_patch = patch("server.sessions.orchestrator.select_account",
                         return_value=Picked("alpha"))
    return env_patch, compose_patch, trust_patch, select_patch, _OrchBuilder(
        db_url, repo_root)


class _OrchBuilder:
    """Builds a started SessionOrchestrator against the harness's DB/repo_root."""

    def __init__(self, db_url, repo_root):
        self.db_url = db_url
        self.repo_root = str(repo_root)

    async def make_orch(self):
        await init_db(db_url=self.db_url)
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
            usage_fn=_fake_usage,
            repo_root=self.repo_root,
        )
        await orch.start()
        return orch


async def _set_global_action(action: str) -> None:
    """Insert/patch the AppSettings singleton so the policy resolves to *action*."""
    from server.db.models import AppSettings
    async with db_session() as db:
        row = (await db.execute(
            select(AppSettings).where(AppSettings.id == "singleton"))).scalar_one_or_none()
        if row is None:
            row = AppSettings(id="singleton")
            db.add(row)
        row.model_approval_action = action


def test_gated_create_holds_pending(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            from server.db.models import Session as DbSession
            orch = await h.make_orch()
            try:
                rec = await orch.create_session(name="s", account=None, model="fable")
                # parked: no capacity slot, no driver
                active = orch._active_session_count()
                in_pending = rec.id in orch._pending_approvals
                driver = orch._drivers.get(rec.id)
                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == rec.id))).scalar_one()
                return rec, active, in_pending, driver, row
            finally:
                await orch.stop()

        rec, active, in_pending, driver, row = asyncio.run(_run())

    assert rec.status == "pending_approval"
    assert rec.pending_model == "fable"
    assert rec.model is None
    assert active == 0
    assert in_pending is True
    assert driver is None
    # persisted parked row
    assert row.status == "pending_approval"
    assert row.pending_model == "fable"
    assert row.model is None
    assert row.pending_model_requested_at is not None


def test_blocked_create_raises(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            orch = await h.make_orch()
            try:
                await _set_global_action("block")
                raised = None
                try:
                    await orch.create_session(name="s", account=None, model="fable")
                except ModelBlockedError as exc:
                    raised = exc
                return raised, len(orch._records)
            finally:
                await orch.stop()

        raised, n_records = asyncio.run(_run())

    assert isinstance(raised, ModelBlockedError)
    assert raised.model == "fable"
    # nothing persisted in-memory for a blocked request
    assert n_records == 0


def test_allowed_model_spawns_normally(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            orch = await h.make_orch()
            try:
                rec = await orch.create_session(name="s", account=None, model="opus")
                return rec.status, rec.model, (rec.id in orch._pending_approvals)
            finally:
                await orch.stop()

        status, model, in_pending = asyncio.run(_run())

    assert status != "pending_approval"
    assert model == "opus"
    assert in_pending is False


# ---------------------------------------------------------------------------
# Task 7 — mid-session gate on set_model_for_session
#
# A running session switches models via orchestrator.set_model_for_session. When
# the requested tier is gated (require_approval) the switch is PARKED: the driver
# is NOT touched, the session keeps running on its current model, and the
# requested model is recorded in record.pending_model + persisted with provenance.
# A blocked tier raises ModelBlockedError; an allowed tier applies as today.
#
# Harness mirrors test_model_switch_restart.py's _FakeDriver (records its
# set_model calls) but wires it into an orchestrator backed by a REAL DB so the
# require_approval persist path (DbSession UPDATE + audit) runs for real.
# ---------------------------------------------------------------------------


class _RecordingDriver:
    """Fake driver that records its set_model/interrupt/send_prompt calls so a
    test can assert the driver was (not) touched. Mirrors test_model_switch_restart
    ._FakeDriver."""
    def __init__(self, turn_active: bool = False):
        self.turn_active = turn_active
        self.model = "current-model"
        self.set_model_calls: list = []
        self.calls: list = []
        # Present so the drain-time transcript scan (_snapshot_open_tasks) treats
        # this fake as a live-but-transcript-less driver instead of raising.
        self.engine_session_id = None

    async def interrupt(self):
        self.calls.append("interrupt")
        self.turn_active = False

    async def set_model(self, m):
        self.set_model_calls.append(m)
        self.calls.append(("set_model", m))
        self.model = m

    async def send_prompt(self, p):
        self.calls.append(("send_prompt", p))


def _running_orch(orch, driver, *, session_id="s-live", model="opus"):
    """Attach a live record + recording driver to an orchestrator built by the
    harness (so db_session() targets the harness DB). Persist a matching DbSession
    row so the require_approval UPDATE has a row to hit."""
    from datetime import datetime
    from server.sessions.engine import AgentEngine
    from server.sessions.types import SessionRecord
    rec = SessionRecord(id=session_id, name="n", account="alpha",
                        engine=AgentEngine.CLAUDE_CODE,
                        engine_session_id="c1", cols=80, rows=24, cwd="/tmp",
                        model=model, updated_at=datetime.utcnow())
    orch._records[session_id] = rec
    orch._drivers[session_id] = driver
    return rec


def test_gated_set_model_holds(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            from server.db.models import Session as DbSession
            orch = await h.make_orch()
            try:
                sid = "s-live"
                async with db_session() as db:
                    db.add(DbSession(id=sid, name="n", account_name="alpha",
                                     engine_session_id="c1", status="idle",
                                     cwd="/tmp", model="opus"))
                driver = _RecordingDriver(turn_active=False)
                rec = _running_orch(orch, driver, session_id=sid, model="opus")

                await orch.set_model_for_session(sid, "fable", identity="user@x")

                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == sid))).scalar_one()
                return rec, driver, row
            finally:
                await orch.stop()

        rec, driver, row = asyncio.run(_run())

    # parked: pending_model set, current model + driver untouched
    assert rec.pending_model == "fable"
    assert rec.model == "opus"
    assert driver.set_model_calls == []
    # persisted with provenance
    assert row.pending_model == "fable"
    assert row.model == "opus"
    assert row.pending_model_requested_by == "user@x"
    assert row.pending_model_requested_at is not None


def test_blocked_set_model_raises(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            orch = await h.make_orch()
            try:
                await _set_global_action("block")
                driver = _RecordingDriver(turn_active=False)
                rec = _running_orch(orch, driver, model="opus")
                raised = None
                try:
                    await orch.set_model_for_session(rec.id, "fable", identity="user@x")
                except ModelBlockedError as exc:
                    raised = exc
                return raised, rec, driver
            finally:
                await orch.stop()

        raised, rec, driver = asyncio.run(_run())

    assert isinstance(raised, ModelBlockedError)
    assert raised.model == "fable"
    assert rec.pending_model is None      # nothing parked
    assert rec.model == "opus"            # current model untouched
    assert driver.set_model_calls == []   # driver never touched


def test_allowed_set_model_applies(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            from server.db.models import Session as DbSession
            orch = await h.make_orch()
            try:
                sid = "s-live"
                async with db_session() as db:
                    db.add(DbSession(id=sid, name="n", account_name="alpha",
                                     engine_session_id="c1", status="idle",
                                     cwd="/tmp", model="opus"))
                driver = _RecordingDriver(turn_active=False)
                rec = _running_orch(orch, driver, session_id=sid, model="opus")

                # opus is not gated → applies as today (driver touched, record updated)
                await orch.set_model_for_session(sid, "sonnet", identity="user@x")
                return rec, driver
            finally:
                await orch.stop()

        rec, driver = asyncio.run(_run())

    assert driver.set_model_calls == ["sonnet"]
    assert rec.model == "sonnet"
    assert rec.pending_model is None


# ---------------------------------------------------------------------------
# Task 8 — resolve_model_approval: approve / deny for BOTH holds.
#
# LAUNCH-TIME hold (a pending_approval, driverless row): approve spawns/enqueues
# the parked model reusing the same session_id; deny closes it to "ended".
# MID-SESSION hold (a running session with record.pending_model): approve applies
# driver.set_model(pending_model); deny clears pending, keeps the current model.
# Both approve AND deny fully clear pending state (record + row).
# ---------------------------------------------------------------------------


def test_approve_launch_spawns(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            from server.db.models import Session as DbSession
            orch = await h.make_orch()
            try:
                rec = await orch.create_session(name="s", account=None, model="fable")
                assert rec.status == "pending_approval"
                out = await orch.resolve_model_approval(
                    rec.id, "approve", identity="user@x")
                in_pending = out.id in orch._pending_approvals
                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == out.id))).scalar_one()
                return out, in_pending, row
            finally:
                await orch.stop()

        out, in_pending, row = asyncio.run(_run())

    assert out.status != "pending_approval"
    assert out.model == "fable"
    assert out.pending_model is None
    assert in_pending is False
    assert row.status != "pending_approval"
    assert row.model == "fable"
    assert row.pending_model is None


def test_deny_launch_ends(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            from server.db.models import Session as DbSession
            orch = await h.make_orch()
            try:
                rec = await orch.create_session(name="s", account=None, model="fable")
                out = await orch.resolve_model_approval(
                    rec.id, "deny", identity="user@x")
                in_pending = out.id in orch._pending_approvals
                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == out.id))).scalar_one()
                return out, in_pending, row
            finally:
                await orch.stop()

        out, in_pending, row = asyncio.run(_run())

    assert out.status == "ended"
    assert out.pending_model is None
    assert in_pending is False
    assert row.status == "ended"
    assert row.pending_model is None


def test_approve_launch_rebuilds_after_restart(tmp_path):
    """The in-memory _pending_approvals entry is lost on a daemon restart; approve
    must rebuild the spawn kwargs from the persisted row and still spawn cleanly."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            from server.db.models import Session as DbSession
            orch = await h.make_orch()
            try:
                rec = await orch.create_session(name="s", account=None, model="fable")
                # Simulate a restart: drop the in-memory spawn kwargs.
                orch._pending_approvals.pop(rec.id, None)
                out = await orch.resolve_model_approval(
                    rec.id, "approve", identity="user@x")
                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == out.id))).scalar_one()
                return out, row
            finally:
                await orch.stop()

        out, row = asyncio.run(_run())

    assert out.status != "pending_approval"
    assert out.model == "fable"
    assert out.pending_model is None
    assert row.model == "fable"
    assert row.pending_model is None


def test_approve_launch_spawn_failure_keeps_session_recoverable(tmp_path):
    """If the spawn raises during an approve+launch (worktree/branch checkout,
    driver.start(), credentials, …), the session must NOT vanish: on failure we
    restore the pending_approval hold (row + in-memory record + spawn kwargs) and
    re-raise. Mirrors
    _maybe_start_queued's spawn-failure durability."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            from server.db.models import Session as DbSession
            orch = await h.make_orch()
            try:
                rec = await orch.create_session(name="s", account=None, model="fable")
                assert rec.status == "pending_approval"

                # Force the launch-time spawn to blow up (as a worktree/branch/
                # driver/credential failure would).
                async def _boom(*a, **k):
                    raise RuntimeError("spawn exploded")
                orch._spawn_session = _boom  # type: ignore[method-assign]

                raised = None
                try:
                    await orch.resolve_model_approval(
                        rec.id, "approve", identity="user@x")
                except RuntimeError as exc:
                    raised = exc

                in_records = rec.id in orch._records
                in_pending = rec.id in orch._pending_approvals
                mem = orch._records.get(rec.id)
                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == rec.id))
                    ).scalar_one_or_none()
                return raised, in_records, in_pending, mem, row
            finally:
                await orch.stop()

        raised, in_records, in_pending, mem, row = asyncio.run(_run())

    # Original spawn exception propagated to the caller.
    assert isinstance(raised, RuntimeError)
    # Session is NOT lost — restored to a recoverable pending_approval hold.
    assert in_records is True
    assert in_pending is True
    assert mem is not None
    assert mem.status == "pending_approval"
    assert mem.pending_model == "fable"
    assert mem.model is None
    # Persisted row survives with the hold restored (not deleted, not ended).
    assert row is not None
    assert row.status == "pending_approval"
    assert row.pending_model == "fable"
    assert row.model is None


def test_approve_launch_never_leaves_the_session_row_absent(tmp_path):
    """The Backlog Runner's orphan sweep (``_release_orphaned_claim``) releases a
    claim the moment its session's ROW is missing, and it runs on the primary
    while the spawn may run on a worker — so a row absent for the spawn's
    worktree/driver setup (~10 s) let the sweep release the issue mid-approve.
    The first prompt then died on StaleOwnership (KinDash#19500, #3139/#3140).
    The row must exist at every point of the approve."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            from server.db.models import Session as DbSession
            orch = await h.make_orch()
            try:
                rec = await orch.create_session(name="s", account=None, model="fable")
                seen: list[str | None] = []
                real_spawn = orch._spawn_session

                async def _probing_spawn(session_id, **kw):
                    async with db_session() as db:
                        seen.append((await db.execute(select(DbSession.status).where(
                            DbSession.id == session_id))).scalar_one_or_none())
                    return await real_spawn(session_id, **kw)
                orch._spawn_session = _probing_spawn  # type: ignore[method-assign]

                out = await orch.resolve_model_approval(rec.id, "approve", identity="user@x")
                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == out.id))).scalar_one()
                return seen, out, row
            finally:
                await orch.stop()

        seen, out, row = asyncio.run(_run())

    assert seen == ["pending_approval"], "the session row vanished during the approve"
    assert out.status != "pending_approval"
    assert row.status != "pending_approval"
    assert row.model == "fable"
    assert row.pending_model is None
    assert row.pending_model_requested_by is None


def test_approve_launch_at_capacity_swaps_the_row_without_a_gap(tmp_path):
    """The at-capacity branch enqueues instead: the placeholder is replaced by the
    queued row inside one transaction, so the row is never missing there either."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            from server.db.models import Session as DbSession
            orch = await h.make_orch()
            try:
                rec = await orch.create_session(name="s", account=None, model="fable")
                orch._active_session_count = lambda: 10_000  # type: ignore[method-assign]
                seen: list[str | None] = []
                real_enqueue = orch._enqueue_session

                async def _probing_enqueue(session_id, **kw):
                    async with db_session() as db:
                        seen.append((await db.execute(select(DbSession.status).where(
                            DbSession.id == session_id))).scalar_one_or_none())
                    return await real_enqueue(session_id, **kw)
                orch._enqueue_session = _probing_enqueue  # type: ignore[method-assign]

                out = await orch.resolve_model_approval(rec.id, "approve", identity="user@x")
                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == out.id))).scalar_one()
                return seen, out, row
            finally:
                await orch.stop()

        seen, out, row = asyncio.run(_run())

    assert seen == ["pending_approval"]
    assert out.status == "queued"
    assert row.status == "queued"
    assert row.model == "fable"
    assert row.pending_model is None


def test_approve_launch_first_prompt_failure_does_not_reopen_the_hold(tmp_path):
    """A failure AFTER the spawn made the session durable (its first prompt
    raising) means the session launched: it has a live driver and a worktree.
    Re-parking it as pending_approval left both behind, and every later Approve
    collided with the leaked worktree ("... already exists", #3139)."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            from server.db.models import Session as DbSession
            orch = await h.make_orch()
            try:
                rec = await orch.create_session(
                    name="s", account=None, model="fable", initial_prompt="go")

                async def _send_prompt_boom(*a, **k):
                    raise RuntimeError("operation no longer owns its targets")
                orch.send_prompt = _send_prompt_boom  # type: ignore[method-assign]

                raised = None
                try:
                    await orch.resolve_model_approval(rec.id, "approve", identity="user@x")
                except RuntimeError as exc:
                    raised = exc
                has_driver = rec.id in orch._drivers
                in_pending = rec.id in orch._pending_approvals
                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == rec.id))).scalar_one()
                return raised, has_driver, in_pending, orch._records[rec.id].status, row
            finally:
                await orch.stop()

        raised, has_driver, in_pending, mem_status, row = asyncio.run(_run())

    assert isinstance(raised, RuntimeError)
    assert has_driver is True
    assert in_pending is False
    assert mem_status != "pending_approval"
    assert row.status != "pending_approval"
    assert row.model == "fable"


def test_approve_launch_spawn_failure_logs_rate_limit_at_info(tmp_path, caplog):
    """A 429/rate-limit failure during approve+launch is an ordinary external
    condition the restore-and-retry path above already handles — the approver
    just tries again. WARNING made every one of these auto-file an error report
    (9 occurrences in 3 days, #1688); anything else (a bad worktree branch, a
    dead credential) is a real defect and must keep WARNING."""
    import logging
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run(exc: Exception):
            orch = await h.make_orch()
            try:
                rec = await orch.create_session(name="s", account=None, model="fable")

                async def _boom(*a, **k):
                    raise exc
                orch._spawn_session = _boom  # type: ignore[method-assign]

                with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
                    try:
                        await orch.resolve_model_approval(
                            rec.id, "approve", identity="user@x")
                    except type(exc):
                        pass
            finally:
                await orch.stop()

        caplog.clear()
        asyncio.run(_run(Exception(
            'API error: 429 {"type":"error","error":{"type":"rate_limit_error",'
            '"message":"This request would exceed your account\'s rate limit."}}')))
        rate_limit_records = [r for r in caplog.records
                              if "session.model_approval_spawn_failed" in r.message]
        assert rate_limit_records, "expected a model_approval_spawn_failed log line"
        assert all(r.levelno == logging.INFO for r in rate_limit_records)

        caplog.clear()
        asyncio.run(_run(RuntimeError("spawn exploded")))
        other_records = [r for r in caplog.records
                         if "session.model_approval_spawn_failed" in r.message]
        assert other_records, "expected a model_approval_spawn_failed log line"
        assert all(r.levelno == logging.WARNING for r in other_records)


def test_approve_midsession_applies(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            from server.db.models import Session as DbSession
            orch = await h.make_orch()
            try:
                sid = "s-live"
                async with db_session() as db:
                    db.add(DbSession(id=sid, name="n", account_name="alpha",
                                     engine_session_id="c1", status="idle",
                                     cwd="/tmp", model="opus"))
                driver = _RecordingDriver(turn_active=False)
                rec = _running_orch(orch, driver, session_id=sid, model="opus")
                await orch.set_model_for_session(sid, "fable", identity="user@x")

                out = await orch.resolve_model_approval(
                    sid, "approve", identity="user@x")

                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == sid))).scalar_one()
                return out, rec, driver, row
            finally:
                await orch.stop()

        out, rec, driver, row = asyncio.run(_run())

    assert driver.set_model_calls[-1] == "fable"
    assert rec.model == "fable"
    assert rec.pending_model is None
    assert out.model == "fable"
    assert row.model == "fable"
    assert row.pending_model is None
    assert row.pending_model_requested_by is None
    assert row.pending_model_requested_at is None


def test_deny_midsession_clears(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            from server.db.models import Session as DbSession
            orch = await h.make_orch()
            try:
                sid = "s-live"
                async with db_session() as db:
                    db.add(DbSession(id=sid, name="n", account_name="alpha",
                                     engine_session_id="c1", status="idle",
                                     cwd="/tmp", model="opus"))
                driver = _RecordingDriver(turn_active=False)
                rec = _running_orch(orch, driver, session_id=sid, model="opus")
                await orch.set_model_for_session(sid, "fable", identity="user@x")

                out = await orch.resolve_model_approval(
                    sid, "deny", identity="user@x")

                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == sid))).scalar_one()
                return out, rec, driver, row
            finally:
                await orch.stop()

        out, rec, driver, row = asyncio.run(_run())

    assert driver.set_model_calls == []   # driver untouched on deny
    assert rec.model == "opus"            # current model kept
    assert rec.pending_model is None
    assert row.model == "opus"
    assert row.pending_model is None
    assert row.pending_model_requested_by is None
    assert row.pending_model_requested_at is None


# ---------------------------------------------------------------------------
# Task 8 fix round 1 (Finding 3) — pending_model_reason clearing and launch-
# branch audit provenance, asserted against the real DB rather than a null
# stub, so a mutation that deletes the clearing/audit code is actually caught.
# ---------------------------------------------------------------------------


def test_deny_midsession_clears_reason(tmp_path):
    """A human-initiated park never itself writes pending_model_reason, so stamp
    one directly (as a stale self-directed reason surviving into a human park
    would) and prove deny wipes it — record AND row."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            from sqlalchemy import update as sa_update
            from server.db.models import Session as DbSession
            orch = await h.make_orch()
            try:
                sid = "s-live"
                async with db_session() as db:
                    db.add(DbSession(id=sid, name="n", account_name="alpha",
                                     engine_session_id="c1", status="idle",
                                     cwd="/tmp", model="opus"))
                driver = _RecordingDriver(turn_active=False)
                rec = _running_orch(orch, driver, session_id=sid, model="opus")
                await orch.set_model_for_session(sid, "fable", identity="user@x")
                rec.pending_model_reason = "stale-reason"
                async with db_session() as db:
                    await db.execute(sa_update(DbSession).where(DbSession.id == sid)
                                     .values(pending_model_reason="stale-reason"))

                out = await orch.resolve_model_approval(sid, "deny", identity="user@x")

                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == sid))).scalar_one()
                return out, row
            finally:
                await orch.stop()

        out, row = asyncio.run(_run())

    assert out.pending_model_reason is None
    assert row.pending_model_reason is None


def test_deny_launch_clears_reason_and_records_audit_provenance(tmp_path):
    """Same reason-clearing proof for the LAUNCH-time hold's deny branch, plus the
    launch-branch audit detail carrying requested/applied (approve and deny)."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            from sqlalchemy import update as sa_update
            from server.db.models import AuditEvent, Session as DbSession
            orch = await h.make_orch()
            try:
                rec = await orch.create_session(name="s", account=None, model="fable")
                rec.pending_model_reason = "stale-reason"
                async with db_session() as db:
                    await db.execute(sa_update(DbSession).where(DbSession.id == rec.id)
                                     .values(pending_model_reason="stale-reason"))

                out = await orch.resolve_model_approval(rec.id, "deny", identity="user@x")

                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == rec.id))).scalar_one()
                    audit_rows = (await db.execute(
                        select(AuditEvent).where(
                            AuditEvent.type == "model_approval.deny"))).scalars().all()
                return out, row, audit_rows
            finally:
                await orch.stop()

        out, row, audit_rows = asyncio.run(_run())

    assert out.pending_model_reason is None
    assert row.pending_model_reason is None
    assert len(audit_rows) == 1
    assert audit_rows[0].detail["requested"] == "fable"
    assert audit_rows[0].detail["applied"] == "fable"


def test_launch_approve_audit_records_requested_and_applied(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            from server.db.models import AuditEvent
            orch = await h.make_orch()
            try:
                rec = await orch.create_session(name="s", account=None, model="fable")
                await orch.resolve_model_approval(rec.id, "approve", identity="user@x")
                async with db_session() as db:
                    rows = (await db.execute(
                        select(AuditEvent).where(
                            AuditEvent.type == "model_approval.approve"))).scalars().all()
                return rows
            finally:
                await orch.stop()

        rows = asyncio.run(_run())

    assert len(rows) == 1
    assert rows[0].detail["requested"] == "fable"
    assert rows[0].detail["applied"] == "fable"


def test_resolve_without_pending_raises(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            orch = await h.make_orch()
            try:
                driver = _RecordingDriver(turn_active=False)
                rec = _running_orch(orch, driver, model="opus")
                raised_value = None
                raised_bad = None
                raised_key = None
                try:
                    await orch.resolve_model_approval(
                        rec.id, "approve", identity="user@x")
                except ValueError as exc:
                    raised_value = exc
                try:
                    await orch.resolve_model_approval(
                        rec.id, "sideways", identity="user@x")
                except ValueError as exc:
                    raised_bad = exc
                try:
                    await orch.resolve_model_approval(
                        "nope", "approve", identity="user@x")
                except KeyError as exc:
                    raised_key = exc
                return raised_value, raised_bad, raised_key
            finally:
                await orch.stop()

        raised_value, raised_bad, raised_key = asyncio.run(_run())

    assert isinstance(raised_value, ValueError)  # nothing pending
    assert isinstance(raised_bad, ValueError)    # bad decision
    assert isinstance(raised_key, KeyError)      # unknown session


# ---------------------------------------------------------------------------
# Inherited default — a session that names NO model still launches on one: the
# `claude` binary reads the shared settings.json `model` key (symlinked into every
# account's config dir), so "Default" is whatever the last `/model` pick wrote
# there. The gate must judge THAT model, not wave None through (2026-09-08:
# Fable 5.1 sessions launched with no approval because "Default" was fable[1m]).
# ---------------------------------------------------------------------------


def _pin_shared_default(tmp_path, model: str) -> None:
    (tmp_path / "dot-claude" / "settings.json").write_text(
        __import__("json").dumps({"model": model}))


def test_inherited_gated_default_holds_pending(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")
        _pin_shared_default(tmp_path, "claude-fable-5-1[1m]")

        async def _run():
            from server.db.models import Session as DbSession
            orch = await h.make_orch()
            try:
                rec = await orch.create_session(name="s", account=None, model=None)
                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == rec.id))).scalar_one()
                return rec, orch._drivers.get(rec.id), row
            finally:
                await orch.stop()

        rec, driver, row = asyncio.run(_run())

    assert rec.status == "pending_approval"
    assert rec.pending_model == "claude-fable-5-1[1m]"
    assert rec.model is None
    assert rec.pending_model_reason  # the approver is told this was inherited
    assert driver is None
    assert row.status == "pending_approval"
    assert row.pending_model == "claude-fable-5-1[1m]"
    assert row.pending_model_reason == rec.pending_model_reason


def test_inherited_default_alias_is_gated_like_no_model(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")
        _pin_shared_default(tmp_path, "fable")

        async def _run():
            orch = await h.make_orch()
            try:
                rec = await orch.create_session(name="s", account=None, model="default")
                return rec.status, rec.pending_model
            finally:
                await orch.stop()

        status, pending = asyncio.run(_run())

    assert status == "pending_approval"
    assert pending == "fable"


def test_inherited_ungated_default_spawns_as_account_default(tmp_path):
    """An inherited opus default is not gated, and the session still records
    model=None — it runs on the account default, which is not a selection."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")
        _pin_shared_default(tmp_path, "opus")

        async def _run():
            orch = await h.make_orch()
            try:
                rec = await orch.create_session(name="s", account=None, model=None)
                return rec.status, rec.model, rec.pending_model, (rec.id in orch._pending_approvals)
            finally:
                await orch.stop()

        status, model, pending, in_pending = asyncio.run(_run())

    assert status != "pending_approval"
    assert model is None
    assert pending is None
    assert in_pending is False


def test_set_model_default_with_gated_inherited_default_holds(tmp_path):
    """Mid-session, switching back to "Default" would hand the binary the same
    shared settings default — so it is gated on that model too."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")
        _pin_shared_default(tmp_path, "claude-fable-5-1[1m]")

        async def _run():
            from server.db.models import Session as DbSession
            orch = await h.make_orch()
            try:
                sid = "s-live"
                async with db_session() as db:
                    db.add(DbSession(id=sid, name="n", account_name="alpha",
                                     engine_session_id="c1", status="idle",
                                     cwd="/tmp", model="opus"))
                driver = _RecordingDriver(turn_active=False)
                rec = _running_orch(orch, driver, session_id=sid, model="opus")
                await orch.set_model_for_session(sid, "default", identity="user@x")
                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == sid))).scalar_one()
                return rec, driver, row
            finally:
                await orch.stop()

        rec, driver, row = asyncio.run(_run())

    assert rec.pending_model == "claude-fable-5-1[1m]"
    assert rec.model == "opus"
    assert driver.set_model_calls == []
    assert row.pending_model == "claude-fable-5-1[1m]"
    assert row.model == "opus"


def test_set_model_default_with_ungated_inherited_default_applies(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")
        _pin_shared_default(tmp_path, "sonnet")

        async def _run():
            from server.db.models import Session as DbSession
            orch = await h.make_orch()
            try:
                sid = "s-live"
                async with db_session() as db:
                    db.add(DbSession(id=sid, name="n", account_name="alpha",
                                     engine_session_id="c1", status="idle",
                                     cwd="/tmp", model="opus"))
                driver = _RecordingDriver(turn_active=False)
                rec = _running_orch(orch, driver, session_id=sid, model="opus")
                await orch.set_model_for_session(sid, "default", identity="user@x")
                return rec, driver
            finally:
                await orch.stop()

        rec, driver = asyncio.run(_run())

    # Not gated → applies exactly as before: the binary is told "default".
    assert driver.set_model_calls == ["default"]
    assert rec.model == "default"
    assert rec.pending_model is None
