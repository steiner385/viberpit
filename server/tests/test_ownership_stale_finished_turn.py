"""#3452: finished runner sessions must not become "Turn failed" when their
automation ownership is stale.

Prod 2026-11-24 05:30 UTC: five Backlog Runner sessions on cairnea/KinDash had
finished their turns with a PR open. The runner released its claim when they were
auto-archived. The archive audit (auto_reopen) then judged each "premature" (red
or unarmed PR) and unarchived it. The first autopilot nudge hit the execution
fence (StaleOwnership: the released operation no longer owns its targets) and
`_pause_session` flipped each to needs_attention, rendered as "Turn failed".
get_session also disagreed with list_sessions on `host` and `prs` for the same
session.

These tests restore that shape (a session with a finished turn and an open PR,
whose runner operation released its target) and pin each half of the fix.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import server.db as database
from server.automations.autopilot_policy import Decision
from server.automations.ownership import (
    CanonicalTarget, StaleOwnership, acquire, execution_refused,
)
from server.db.models import (
    Automation, AutomationOperation, AutomationOwnedTarget, AutomationTargetState, Base,
    Session as DbSession,
)
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


@pytest_asyncio.fixture
async def ownership_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/own.db")
    tables = [DbSession.__table__, Automation.__table__, AutomationTargetState.__table__,
              AutomationOperation.__table__, AutomationOwnedTarget.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "_session_factory",
                        async_sessionmaker(engine, expire_on_commit=False))
    try:
        yield
    finally:
        await engine.dispose()


async def _runner_session(sid: str, *, release: bool) -> None:
    """A runner-owned session row; ``release`` closes the claim out as archive does."""
    async with database.db_session() as db:
        db.add(DbSession(id=sid, name="n", account_name="a", status="archived",
                         cwd="/tmp"))
        await db.commit()
    target = CanonicalTarget(node_id=f"I_{sid}", display_ref=f"o/r#{sid}")
    async with acquire([target], owner="dobby:runner", source="backlog-runner",
                       reason="dispatch", provenance={"trigger": "test"}) as tx:
        tx.start_action(session_id=sid)
        token = tx.token
    if release:
        # The end state the runner's close-out leaves (a WORK_STOPPED release, whose
        # attestation dance is test_automation_ownership's subject, not this one's).
        from sqlalchemy import update
        async with database.db_session() as db:
            await db.execute(update(AutomationOwnedTarget)
                             .where(AutomationOwnedTarget.key == target.key)
                             .values(operation_token=None))
            await db.execute(update(AutomationOperation)
                             .where(AutomationOperation.token == token)
                             .values(phase="released"))
            await db.commit()


# ── the predicate the audit asks before reopening ────────────────────────────

@pytest.mark.asyncio
async def test_execution_refused_tracks_the_fence(ownership_db):
    await _runner_session("released", release=True)
    await _runner_session("held", release=False)
    async with database.db_session() as db:
        db.add(DbSession(id="human", name="n", account_name="a", status="idle", cwd="/tmp"))
        await db.commit()

    assert await execution_refused("released") is True, (
        "a released runner operation still names the session: every start is refused")
    assert await execution_refused("held") is False, "a live claim still owns its target"
    assert await execution_refused("human") is False, "no operation, no fence"


# ── the archive audit no longer reopens a fenced session ─────────────────────

def test_archive_audit_does_not_reopen_a_fenced_session():
    from server.automations.archive_audit import ArchiveAuditLoop

    unarchived: list[str] = []
    reverted: list[str] = []
    emitted: list = []

    async def _read_config():
        return "auto_reopen", {"window_days": 7}

    async def _list_rows(_since):
        return [{"id": sid, "goal": "", "repo": None} for sid in ("fenced", "free")]

    async def _findings(_results, _mode):
        return {"to_unarchive": ["fenced", "free"], "open_total": 0}

    async def _unarchive(sid):
        unarchived.append(sid)

    async def _revert(sid):
        reverted.append(sid)

    async def _refused(sid):
        return sid == "fenced"

    async def _emit(*a, **k):
        emitted.append((a, k))

    ctx = SimpleNamespace(emit=_emit)
    loop = ArchiveAuditLoop(
        ctx, read_config=_read_config, list_rows=_list_rows, gh_fn=lambda r, b: [],
        gh_num_fn=None, unarchive_fn=_unarchive, clock=lambda: 0.0,
        record_findings_fn=_findings, mark_auto_reopen_failed_fn=_revert,
        reopen_refused_fn=_refused)
    asyncio.run(loop.tick())

    assert unarchived == ["free"], "a fenced session must never be reopened"
    assert reverted == ["fenced"], "its finding goes back to a human, not 'reopened'"
    assert ctx.archive_audit_counts["fenced"] == 1


# ── autopilot keeps a finished turn's status when the fence refuses ──────────

def _bare_orch(rec: SessionRecord):
    import server.sessions.orchestrator as orch_mod
    orch: Any = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._draining = False
    orch._records = {rec.id: rec}
    orch._turn_restart_at = {}
    orch.events = []
    orch._publish_event = orch.events.append
    orch.persisted = []

    async def _persist_status(sid, status):
        # The status write that branded these "Turn failed": recorded and applied to
        # the record, as the real one does, without its driver-side overlays.
        orch.persisted.append(status)
        orch._records[sid].status = status
        return status

    orch._persist_status = _persist_status

    async def _hosts():
        return None

    async def _may_act(*_a, **_k):
        return True

    async def _ensure_driver(*_a, **_k):
        raise StaleOwnership("operation no longer owns its targets")

    orch._enabled_host_names = _hosts
    orch._owns_record = lambda r, h: True
    orch._autopilot_may_act = _may_act
    orch.ensure_driver = _ensure_driver
    return orch


async def _seed(sid: str, status: str) -> SessionRecord:
    async with database.db_session() as db:
        db.add(DbSession(id=sid, name="n", account_name="a", status=status, cwd="/tmp"))
        await db.commit()
    return SessionRecord(id=sid, name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                         engine_session_id="c1", cols=80, rows=24, cwd="/tmp",
                         status=status, origin="backlog-runner")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["idle", "done", "ready_review", "handed_back"])
async def test_finished_turn_keeps_its_status_on_stale_ownership(ownership_db, status):
    rec = await _seed("s1", status)
    orch = _bare_orch(rec)

    ok = await orch._autopilot_apply("s1", Decision(action="nudge", prompt="arm the PR"))

    assert ok is False
    assert "needs_attention" not in orch.persisted
    assert rec.status == status, "a finished turn must not be rebranded 'Turn failed'"
    assert rec.paused_reason == "automation_ownership_stale"
    async with database.db_session() as db:
        row = await db.get(DbSession, "s1")
    assert (row.status, row.paused_reason) == (status, "automation_ownership_stale")
    assert not [e for e in orch.events if e.get("type") == "paused"]
    # The reason alone keeps autopilot off it, so it is not re-nudged every tick.
    import server.sessions.orchestrator as orch_mod
    assert rec.paused_reason in orch_mod._TERMINAL_PAUSE_REASONS


@pytest.mark.asyncio
async def test_already_parked_session_still_pauses(ownership_db):
    rec = await _seed("s2", "needs_attention")
    orch = _bare_orch(rec)
    await orch._autopilot_apply("s2", Decision(action="nudge", prompt="go"))
    # Already parked: no finished turn to protect, so the existing pause stands.
    assert orch.persisted == ["needs_attention"]
    assert rec.paused_reason == "automation_ownership_stale"
    assert [e["reason"] for e in orch.events if e.get("type") == "paused"] == [
        "automation_ownership_stale"]


# ── get_session reads like list_sessions ─────────────────────────────────────

class _HostOrch:
    def __init__(self, rec):
        self._rec = rec

    async def get_session(self, session_id):
        return self._rec


class _Router:
    def __init__(self, orchs):
        self._orchs = orchs

    def get(self, name):
        return self._orchs[name]

    def names(self):
        return sorted(self._orchs)


@pytest.mark.asyncio
async def test_fleet_get_session_labels_an_unstamped_record_with_its_host(monkeypatch):
    from server.sessions.fleet_orchestrator import FleetOrchestrator
    rec = SimpleNamespace(id="s1", host=None)
    fo = FleetOrchestrator(_Router({"dobby": _HostOrch(rec), "rocky": _HostOrch(None)}),  # type: ignore[arg-type]
                           primary="dobby")

    async def _no_stamp(_sid):
        return None

    monkeypatch.setattr(fo, "_host_for", _no_stamp)
    got = await fo.get_session("s1")
    assert got.host == "dobby", "list_sessions labels it with the serving host; so must get"


@pytest.mark.asyncio
async def test_get_session_api_carries_the_linked_prs(monkeypatch):
    from server.api import sessions as api
    from server.github.pr_status import PullRequestInfo

    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                        engine_session_id="c1", cols=80, rows=24, cwd="", status="idle")
    pr = PullRequestInfo(21353, "https://github.com/o/r/pull/21353", "open", "failing", "t")

    class _Orch:
        async def get_session(self, sid):
            return rec

        async def prs_for_many(self, ids, **_k):
            return {sid: [pr] for sid in ids}

    async def _snap():
        raise RuntimeError("no heartbeat table here")

    monkeypatch.setattr(api.host_heartbeat, "snapshot", _snap)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(sessions=_Orch())),
                              query_params={})
    body = await api.get_session("s1", request, _identity="u")  # type: ignore[arg-type]
    assert [p["number"] for p in body["prs"]] == [21353]
