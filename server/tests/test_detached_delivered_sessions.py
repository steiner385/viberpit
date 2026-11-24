"""#3459: detached sessions that delivered their work, and stranded siblings.

Prod 2026-11-24: twelve KinDash sessions sat at "Turn failed" although each had
finished its turn and opened, armed or merged its PR. Their owner operation had
been released — once by the archive audit's reopen, once after the operator paused
PR Doctor and bulk-archived some of its sessions — and a released operation fences
every session it still names forever.

Pinned here, each against the real ownership store on SQLite:

* a session detached with a delivered PR (merged / armed / merge queue) is
  auto-archived; one with an open, unarmed PR is not;
* a restart during a finished turn with an open PR reattaches the session's
  ownership on the first (boot) pass;
* pausing an automation leaves its sessions alone, and resuming it reattaches them;
* bulk-archiving some sibling sessions of one operation no longer strands the
  live one, and the revived claim is released again once it too is archived.
"""
from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import server.db as database
from server.automations.completion_bar import PrFact, PrFacts
from server.automations.ownership import (
    REATTACHED_PROVENANCE_KEY, CanonicalTarget, acquire, begin_session_execution,
    execution_refused, reattach_session, release_reattached_if_stopped,
)
from server.db.models import (
    Automation, AutomationOperation, AutomationOwnedTarget, AutomationTargetState, Base,
    Session as DbSession,
)
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord

STALE = "automation_ownership_stale"


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
    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)
    try:
        yield
    finally:
        await engine.dispose()


async def _set_automation(owner: str, enabled: bool) -> None:
    async with database.db_session() as db:
        row = await db.get(Automation, owner)
        if row is None:
            db.add(Automation(id=owner, enabled=enabled))
        else:
            row.enabled = enabled


async def _operation(sessions: dict[str, str], *, owner: str = "backlog-runner",
                     node: str = "I_issue", release: bool = True) -> tuple[str, str]:
    """One operation naming every session in *sessions* (id → row status), each with
    one recorded execution; ``release`` settles the claim the way the owner does when
    its sessions are archived. Returns (token, target key)."""
    async with database.db_session() as db:
        for sid, status in sessions.items():
            db.add(DbSession(id=sid, name="n", account_name="a", status=status, cwd="/tmp"))
    root = next(iter(sessions))
    target = CanonicalTarget(node_id=node, display_ref=f"o/r#{node}")
    async with acquire([target], owner=owner, source="test", reason="dispatch") as tx:
        tx.start_action(session_id=root)
        tx.operation.execution_sessions = {
            sid: {"current": sid, "executions": {sid: {"generation": i + 1, "stopped": i + 1}}}
            for i, sid in enumerate(sessions)}
        tx.operation.execution_generation = len(sessions)
        tx.operation.stopped_generation = len(sessions)
        token = tx.token
    if release:
        async with database.db_session() as db:
            await db.execute(update(AutomationOwnedTarget)
                             .where(AutomationOwnedTarget.key == target.key)
                             .values(operation_token=None))
            await db.execute(update(AutomationOperation)
                             .where(AutomationOperation.token == token)
                             .values(phase="released", reason="merged close-out"))
    return token, target.key


async def _holder(key: str) -> str | None:
    async with database.db_session() as db:
        row = await db.get(AutomationOwnedTarget, key)
    assert row is not None
    return row.operation_token


async def _phase(token: str) -> str:
    async with database.db_session() as db:
        op = await db.get(AutomationOperation, token)
    assert op is not None
    return op.phase


async def _archive_row(sid: str) -> None:
    async with database.db_session() as db:
        await db.execute(update(DbSession).where(DbSession.id == sid)
                         .values(status="archived"))


# ── the store: reattach is narrow, and its release half closes the loop ───────

@pytest.mark.asyncio
async def test_reattach_revives_a_released_operation_for_a_live_session(ownership_db):
    token, key = await _operation({"live": "idle"})
    assert await execution_refused("live") is True

    owner = await reattach_session("live", owners={"backlog-runner"}, reason="reattached: test")

    assert owner == "backlog-runner"
    assert await _holder(key) == token
    assert await _phase(token) == "active"
    assert await execution_refused("live") is False
    await begin_session_execution("live")   # the fence lets a driver start again
    async with database.db_session() as db:
        op = await db.get(AutomationOperation, token)
    assert op.provenance[REATTACHED_PROVENANCE_KEY] == "merged close-out"


@pytest.mark.asyncio
async def test_reattach_refuses_when_the_work_moved_on(ownership_db):
    _, key = await _operation({"live": "idle"})
    # The issue was claimed again by a new operation: that one owns the work now.
    other = CanonicalTarget(node_id="I_issue", display_ref="o/r#I_issue")
    async with acquire([other], owner="backlog-runner", source="test", reason="re-claim") as tx:
        new_token = tx.token

    assert await reattach_session("live", owners={"backlog-runner"}, reason="r") is None
    assert await _holder(key) == new_token
    assert await execution_refused("live") is True


@pytest.mark.asyncio
async def test_reattach_refuses_a_paused_owner_and_an_archived_session(ownership_db):
    token, key = await _operation({"live": "idle", "gone": "archived"}, node="I_a")
    assert await reattach_session("live", owners={"pr-doctor"}, reason="r") is None
    assert await reattach_session("gone", owners={"backlog-runner"}, reason="r") is None
    assert await _holder(key) is None and await _phase(token) == "released"


# ── a bulk archive of siblings no longer strands the live one ────────────────

@pytest.mark.asyncio
async def test_bulk_archive_of_siblings_does_not_strand_the_last_one(ownership_db):
    # Three sessions under one operation; the operator archives two, the owner
    # settles its claim, and the third is fenced — the 11:33 UTC batch.
    token, key = await _operation({"a": "idle", "b": "idle", "c": "done"})
    await _archive_row("a")
    await _archive_row("b")
    assert await execution_refused("c") is True

    assert await reattach_session("c", owners={"backlog-runner"}, reason="r") == "backlog-runner"
    assert await execution_refused("c") is False
    assert await release_reattached_if_stopped() == [], "c is still live: keep the claim"
    assert await _holder(key) == token

    await _archive_row("c")
    assert await release_reattached_if_stopped() == [token]
    assert await _holder(key) is None, "the revived claim must not hold the issue forever"
    assert await _phase(token) == "released"


@pytest.mark.asyncio
async def test_release_half_ignores_operations_it_never_reattached(ownership_db):
    token, key = await _operation({"s": "archived"}, release=False)
    assert await release_reattached_if_stopped() == []
    assert await _holder(key) == token


# ── the orchestrator: boot, pause/resume, delivered auto-archive ─────────────

def _orch(*recs: SessionRecord, facts: dict[str, PrFacts] | None = None):
    import server.sessions.orchestrator as orch_mod
    orch: Any = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._draining = False
    orch._records = {r.id: r for r in recs}
    orch._auto_archiving = set()
    orch.archived = []

    async def _hosts():
        return None

    async def _archive_enabled():
        return True

    async def _archive(sid, *, archived_by="user", context=None):
        orch.archived.append((sid, archived_by, context))
        orch._records[sid].status = "archived"

    orch._enabled_host_names = _hosts
    orch._owns_record = lambda r, h: True
    orch._read_auto_archive_enabled = _archive_enabled
    orch.archive_session = _archive
    orch._autopilot_pr_facts = lambda rec: (facts or {}).get(rec.id, PrFacts(has_repo=True))
    return orch


def _rec(sid: str, status: str, *, reason: str | None = STALE) -> SessionRecord:
    rec = SessionRecord(id=sid, name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                        engine_session_id="c1", cols=80, rows=24, cwd="/tmp",
                        status=status, origin="backlog-runner", repo="o/r")
    rec.paused_reason = reason
    return rec


def _pr(number: int, state: str = "open", *, armed: bool = False,
        queued: bool = False) -> PrFacts:
    return PrFacts(has_repo=True, prs=(PrFact(
        number=number, state=state, is_draft=False, auto_merge_enabled=armed,
        url=f"u/{number}", in_merge_queue=queued),))


async def _stamp_stale(sid: str) -> None:
    async with database.db_session() as db:
        await db.execute(update(DbSession).where(DbSession.id == sid)
                         .values(paused_reason=STALE))


@pytest.mark.asyncio
async def test_restart_during_a_finished_turn_with_an_open_pr_reattaches(ownership_db):
    await _set_automation("backlog-runner", True)
    token, key = await _operation({"s1": "done"})
    await _stamp_stale("s1")
    rec = _rec("s1", "done")
    orch = _orch(rec, facts={"s1": _pr(21353)})   # open, unarmed: nothing delivered

    # The archive pass runs first in the tick and must leave an undelivered PR alone.
    assert await orch._archive_delivered_detached_once() == 0
    assert await orch._reattach_stale_ownership_once() == 1   # first pass = boot

    assert rec.status == "done" and rec.paused_reason is None
    async with database.db_session() as db:
        assert (await db.get(DbSession, "s1")).paused_reason is None
    assert await _holder(key) == token
    assert await execution_refused("s1") is False
    assert orch.archived == []


@pytest.mark.asyncio
async def test_pause_then_resume_an_automation_with_live_sessions(ownership_db):
    await _set_automation("pr-doctor", False)
    await _operation({"d1": "idle"}, owner="pr-doctor", node="I_d1")
    await _operation({"d2": "handed_back"}, owner="pr-doctor", node="I_d2")
    for sid in ("d1", "d2"):
        await _stamp_stale(sid)
    recs = [_rec("d1", "idle"), _rec("d2", "handed_back")]
    orch = _orch(*recs)

    assert await orch._reattach_stale_ownership_once() == 0, "paused: nothing reattached"
    assert all(r.paused_reason == STALE for r in recs)
    assert await orch._reattach_stale_ownership_once() == 0, "still paused"

    await _set_automation("pr-doctor", True)
    assert await orch._reattach_stale_ownership_once() == 2, "re-enabled: both come back"
    assert all(r.paused_reason is None for r in recs)
    assert not [sid for sid in ("d1", "d2") if await execution_refused(sid)]

    # Edge-triggered, not level: while the owner stays enabled, a release it makes
    # itself (a give-up, so the runner can re-dispatch the issue) is its decision,
    # and the next tick must not take the claim back from it.
    async with database.db_session() as db:
        await db.execute(update(AutomationOwnedTarget)
                         .where(AutomationOwnedTarget.key == "github:I_d1")
                         .values(operation_token=None))
        await db.execute(update(AutomationOperation)
                         .where(AutomationOperation.owner == "pr-doctor",
                                AutomationOperation.session_id == "d1")
                         .values(phase="released"))
    recs[0].paused_reason = STALE
    assert await orch._reattach_stale_ownership_once() == 0
    assert recs[0].paused_reason == STALE


@pytest.mark.asyncio
async def test_reattach_skips_needs_attention_and_unfenced_sessions(ownership_db):
    await _set_automation("backlog-runner", True)
    await _operation({"failed": "needs_attention"}, node="I_f")
    await _operation({"human": "idle"}, node="I_h")
    recs = [_rec("failed", "needs_attention"), _rec("human", "idle", reason=None)]
    orch = _orch(*recs)

    assert await orch._reattach_stale_ownership_once() == 0
    assert recs[0].paused_reason == STALE, (
        "clearing it would hand a needs_attention row to the stuck-session sweep")


@pytest.mark.asyncio
@pytest.mark.parametrize("facts,how", [
    (_pr(9, "merged"), "merged"),
    (_pr(9, armed=True), "armed to merge"),
    (_pr(9, queued=True), "armed to merge"),
])
async def test_detached_session_with_delivered_pr_is_auto_archived(ownership_db, facts, how):
    rec = _rec("s", "needs_attention")
    orch = _orch(rec, facts={"s": facts})

    assert await orch._archive_delivered_detached_once() == 1

    [(sid, archived_by, context)] = orch.archived
    assert (sid, archived_by) == ("s", "completion")
    assert context["reason"] == f"detached: work delivered (PR #9 {how})"
    assert context["pr_state"]["prs"][0]["number"] == 9
    assert "s" not in orch._auto_archiving


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["not_stale", "opted_out", "snoozed", "undelivered",
                                    "toggle_off", "mid_turn"])
async def test_detached_auto_archive_honours_every_opt_out(ownership_db, change):
    rec = _rec("s", "in_progress" if change == "mid_turn" else "done",
               reason=None if change == "not_stale" else STALE)
    if change == "opted_out":
        rec.auto_archive_opt_out_reason = "user-unarchived"
    if change == "snoozed":
        rec.snooze = {"until": "2099-01-01T00:00:00Z"}
    orch = _orch(rec, facts={"s": _pr(9) if change == "undelivered" else _pr(9, "merged")})
    if change == "toggle_off":
        async def _off():
            return False
        orch._read_auto_archive_enabled = _off

    assert await orch._archive_delivered_detached_once() == 0
    assert orch.archived == []


def test_delivered_pr_prefers_merged_and_excludes_drafts():
    merged = PrFact(number=2, state="merged", is_draft=False, auto_merge_enabled=False, url="u")
    armed = PrFact(number=1, state="open", is_draft=False, auto_merge_enabled=True, url="u")
    draft = PrFact(number=3, state="open", is_draft=True, auto_merge_enabled=True, url="u")
    assert PrFacts(True, (armed, merged)).delivered_pr is merged
    assert PrFacts(True, (armed,)).delivered_pr is armed
    assert PrFacts(True, (draft,)).delivered_pr is None
    assert PrFacts(True, ()).delivered_pr is None


@pytest.mark.asyncio
async def test_enabled_automation_ids_reads_the_toggle(ownership_db):
    await _set_automation("pr-doctor", True)
    await _set_automation("backlog-runner", False)
    orch = _orch()
    assert await orch._enabled_automation_ids() == frozenset({"pr-doctor"})
    async with database.db_session() as db:
        assert (await db.scalars(select(Automation.id))).all()
