"""FR-305 / FR-406 (spec 002, #947): a pending permission, question or plan approval
is a durable ``session_holds`` row while it is open, and a restart that kills its
waiter leaves a ``hold_lost`` timeline event carrying the content snapshot.

Before this the ask lived only in the daemon's memory (``PermissionRegistry`` /
``QuestionRegistry``), so after a restart the card could say "interrupted" but never
WHAT was being asked.
"""
from __future__ import annotations

import asyncio
import json
import logging
import types
from unittest.mock import patch

from sqlalchemy import select as sa_select

from server.api.sessions import _record_to_dict
from server.db import db_session, dispose_db, init_db
from server.db.models import Session as DbSession
from server.db.models import SessionHold
from server.sessions import holds, orphaned_asks, provenance
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory

_QUESTIONS = [{
    "question_id": "q1", "turn_id": "t1", "header": "Pick", "question": "Which?",
    "options": [{"label": "A", "description": "first"}, {"label": "B"}],
    "multi_select": False, "parent_tool_use_id": None,
}]


def _card(pid: str, tool: str, input: dict, *, items=None) -> dict:
    """What ``PermissionRegistry.request`` parks in ``_pending`` (the keys
    ``pending()`` reads)."""
    return {"session_id": "s1", "tool": tool, "input": input, "turn_id": "t1",
            "identity": "me", "items": items}


async def _build_orch(tmp_path, *, status: str = "idle", questions=None,
                      seed_hold: tuple[str, dict] | None = None) -> SessionOrchestrator:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'holds.db'}"
    await init_db(db_url=db_url)
    async with db_session() as db:
        db.add(DbSession(id="s1", name="New Session", account_name="acct1",
                         engine_session_id="claude-s1", status=status, cwd="/repo/s1"))
    if seed_hold is not None:
        assert await holds.open_hold("s1", *seed_hold) is not None
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    orch._drivers["s1"] = types.SimpleNamespace(
        last_hand_back_reason=None,
        pending_questions=lambda: list(questions or []))
    return orch


async def _rows(sid: str = "s1") -> list[SessionHold]:
    async with db_session() as db:
        return list((await db.execute(
            sa_select(SessionHold).where(SessionHold.session_id == sid)
            .order_by(SessionHold.created_at))).scalars().all())


async def _teardown(orch: SessionOrchestrator) -> None:
    await orch.stop()
    await dispose_db()


# ---------------------------------------------------------------------------
# (1) a question hold opens once, survives re-persists, resolves on the next status
# ---------------------------------------------------------------------------

def test_question_hold_opens_once_and_resolves_when_the_turn_moves_on(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path, questions=_QUESTIONS)
        try:
            await orch._persist_status("s1", "waiting_question")
            after_open = await _rows()
            await orch._persist_status("s1", "waiting_question")   # per-derive re-persist
            after_repeat = await _rows()
            await orch._persist_status("s1", "in_progress")
            after_resolve = await _rows()
            return after_open, after_repeat, after_resolve, dict(orch._open_hold_kind)
        finally:
            await _teardown(orch)

    after_open, after_repeat, after_resolve, open_kinds = asyncio.run(run())
    assert len(after_open) == 1
    h = after_open[0]
    assert h.kind == "question" and h.resolved_at is None and h.created_at is not None
    assert h.content == {
        "questions": [{"questionId": "q1", "header": "Pick", "question": "Which?",
                       "options": [{"label": "A", "description": "first"}, {"label": "B"}],
                       "multiSelect": False}],
        "turnId": "t1",
    }
    assert len(after_repeat) == 1 and after_repeat[0].resolved_at is None
    assert len(after_resolve) == 1 and after_resolve[0].resolved_at is not None
    assert open_kinds == {}


# ---------------------------------------------------------------------------
# (2) permission and plan holds snapshot the registry card
# ---------------------------------------------------------------------------

def test_permission_then_plan_holds_snapshot_the_newest_card(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        try:
            orch._registry._pending["p1"] = _card("p1", "Bash", {"command": "make test"})
            await orch._persist_status("s1", "waiting_input")
            after_perm = await _rows()
            # The permission is answered; the turn goes on to ask for plan approval.
            orch._registry._pending.pop("p1")
            orch._registry._pending["p2"] = _card(
                "p2", "ExitPlanMode", {"plan": "1. do it"}, items=["1. do it"])
            await orch._persist_status("s1", "ready_review")
            after_plan = await _rows()
            return after_perm, after_plan
        finally:
            await _teardown(orch)

    after_perm, after_plan = asyncio.run(run())
    assert [(h.kind, h.resolved_at is None) for h in after_perm] == [("permission", True)]
    assert after_perm[0].content == {"tool": "Bash", "input": {"command": "make test"},
                                     "items": None, "turnId": "t1", "permId": "p1"}
    # A different kind closes the old hold and opens a new one.
    assert [(h.kind, h.resolved_at is None) for h in after_plan] == [
        ("permission", False), ("plan", True)]
    assert after_plan[1].content["tool"] == "ExitPlanMode"
    assert after_plan[1].content["permId"] == "p2"
    assert after_plan[1].content["items"] == ["1. do it"]


def test_the_registry_owner_never_reaches_the_hold_row(tmp_path):
    """``pending()`` carries the resolving identity; a durable row must not."""
    async def run():
        orch = await _build_orch(tmp_path)
        try:
            orch._registry._pending["p1"] = _card("p1", "Bash", {"command": "ls"})
            await orch._persist_status("s1", "waiting_input")
            return (await _rows())[0].content
        finally:
            await _teardown(orch)

    content = asyncio.run(run())
    assert "owner" not in json.dumps(content) and "me" not in json.dumps(content).split('"')


# ---------------------------------------------------------------------------
# (3) a shutdown's fake exit must NOT resolve the hold
# ---------------------------------------------------------------------------

def test_shutdown_keeps_the_hold_open_for_the_next_boot(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path, questions=_QUESTIONS)
        try:
            await orch._persist_status("s1", "waiting_question")
            orch._shutting_down = True
            # The restart's deny_all settles the ask and the driver derives a
            # non-hold status — that is the daemon dying, not the user answering.
            await orch._persist_status("s1", "in_progress")
            return await _rows()
        finally:
            await _teardown(orch)

    rows = asyncio.run(run())
    assert [(h.kind, h.resolved_at) for h in rows] == [("question", None)]


# ---------------------------------------------------------------------------
# (4) restart: an open hold on an interrupted row is reported lost
# ---------------------------------------------------------------------------

def test_rehydrate_reports_an_interrupted_hold_lost_with_its_content(tmp_path):
    content = holds.snapshot_content("question", [], _QUESTIONS)

    async def run():
        orch = await _build_orch(tmp_path, status="waiting_question",
                                 seed_hold=("question", content))
        try:
            rec = orch.record("s1")
            events = await provenance.list_events("s1")
            return (rec, await _rows(), events, rec.to_dict(),
                    _record_to_dict(rec, repo=None))
        finally:
            await _teardown(orch)

    rec, rows, events, rpc, http = asyncio.run(run())
    assert rec.status == "idle" and rec.interrupted_by_restart
    assert len(rows) == 1 and rows[0].resolved_at is not None
    assert [e["kind"] for e in events] == [provenance.HOLD_LOST]
    detail = events[0]["detail"]
    assert detail["kind"] == "question" and detail["content"] == content
    assert detail["holdId"] == rows[0].id and detail["createdAt"]
    assert rec.lost_hold == detail
    # Two-serializer rule: daemon→edge RPC and the browser payload both carry it.
    assert rpc["lostHold"] == detail
    assert http["lostHold"] == detail
    assert SessionRecord.from_dict(rpc).lost_hold == detail


def test_lost_hold_clears_when_the_session_does_something_again(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path, status="waiting_input",
                                 seed_hold=("permission", {"tool": "Bash"}))
        try:
            rec = orch.record("s1")
            assert rec.lost_hold is not None
            await orch._persist_status("s1", "idle")
            still = rec.lost_hold
            await orch._persist_status("s1", "in_progress")
            return still, rec.lost_hold, dict(orch._open_hold_kind)
        finally:
            await _teardown(orch)

    still, cleared, open_kinds = asyncio.run(run())
    assert still is not None      # idle is "nothing happened yet"
    assert cleared is None
    assert open_kinds == {}


# ---------------------------------------------------------------------------
# (5) a stale hold on a resting row is closed silently
# ---------------------------------------------------------------------------

def test_rehydrate_resolves_a_stale_hold_on_a_resting_row_without_an_event(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path, status="idle",
                                 seed_hold=("plan", {"tool": "ExitPlanMode"}))
        try:
            rec = orch.record("s1")
            return rec.lost_hold, await _rows(), await provenance.list_events("s1")
        finally:
            await _teardown(orch)

    lost, rows, events = asyncio.run(run())
    assert lost is None
    assert len(rows) == 1 and rows[0].resolved_at is not None
    assert events == []


# ---------------------------------------------------------------------------
# (6) snapshot_content: shape, newest-card choice, truncation
# ---------------------------------------------------------------------------

def test_snapshot_picks_the_plan_card_for_a_plan_hold():
    perms = [{"permId": "p1", "tool": "Bash", "input": {"command": "ls"}, "turn_id": "t1",
              "items": None, "owner": "me"},
             {"permId": "p2", "tool": "ExitPlanMode", "input": {"plan": "x"}, "turn_id": "t1",
              "items": ["x"], "owner": "me"},
             {"permId": "p3", "tool": "Read", "input": {"file_path": "/f"}, "turn_id": "t1",
              "items": None, "owner": "me"}]
    plan = holds.snapshot_content("plan", perms, [])
    assert plan["tool"] == "ExitPlanMode" and plan["permId"] == "p2"
    # A permission hold takes the newest non-plan card.
    perm = holds.snapshot_content("permission", perms, [])
    assert perm["tool"] == "Read" and perm["permId"] == "p3"
    assert "owner" not in perm and "truncated" not in perm


def test_snapshot_with_nothing_pending_is_an_empty_but_typed_content():
    assert holds.snapshot_content("permission", [], []) == {
        "tool": None, "input": None, "items": None, "turnId": None, "permId": None}
    assert holds.snapshot_content("question", [], []) == {"questions": [], "turnId": None}


def test_snapshot_caps_long_strings_and_the_whole_payload():
    one = holds.snapshot_content(
        "permission",
        [{"permId": "p", "tool": "Write", "turn_id": "t",
          "input": {"file_path": "/f", "content": "x" * 10_000, "nested": {"deep": "y" * 5000}},
          "items": None}], [])
    assert len(one["input"]["content"]) == holds.MAX_STRING_CHARS
    assert len(one["input"]["nested"]["deep"]) == holds.MAX_STRING_CHARS
    assert one["input"]["file_path"] == "/f"
    assert one["truncated"] is True

    many = holds.snapshot_content(
        "permission",
        [{"permId": "p", "tool": "Write", "turn_id": "t",
          "input": {f"k{i}": "z" * 4000 for i in range(20)}, "items": ["i" * 4000] * 5}], [])
    assert len(json.dumps(many)) <= holds.MAX_CONTENT_BYTES
    assert many["truncated"] is True
    assert many["tool"] == "Write" and many["permId"] == "p"   # identity survives


# ---------------------------------------------------------------------------
# vocabulary + never-raise contract
# ---------------------------------------------------------------------------

def test_hold_lost_is_in_the_provenance_vocabulary():
    assert provenance.HOLD_LOST == "hold_lost"
    assert provenance.HOLD_LOST in provenance.KINDS


def test_hold_kinds_come_from_the_orphaned_ask_map():
    """One map, two readers: the restart card and the durable hold agree by construction."""
    assert holds.HOLD_KIND_BY_STATUS is orphaned_asks.HOLD_KIND_BY_STATUS
    assert set(holds.HOLD_KIND_BY_STATUS.values()) == {"permission", "question", "plan"}


def test_hold_writes_swallow_db_failure_at_info(isolated_db, caplog):
    """INFO, not WARNING: a WARNING auto-files an issue, and one outage during a hold
    transition already produces the status persist's own warning (FR-307)."""
    def boom():
        raise RuntimeError("db down")

    with patch("server.sessions.holds.db_session", boom), \
            caplog.at_level(logging.INFO, logger="server.sessions.holds"):
        opened = asyncio.run(holds.open_hold("s9", "question", {}))
        resolved = asyncio.run(holds.resolve_open("s9"))
        newest = asyncio.run(holds.newest_open(["s9"]))
    assert opened is None and resolved == 0 and newest == {}
    mine = [r for r in caplog.records if "holds." in r.getMessage()]
    assert len(mine) == 3 and all(r.levelno == logging.INFO for r in mine)


def test_snapshot_is_json_native_even_for_a_non_json_tool_input(tmp_path):
    """A Path/datetime inside ``input`` must not fail every open for the session:
    the snapshot coerces once, so the JSON column's encoder never sees them."""
    import datetime as _dt
    import pathlib

    content = holds.snapshot_content(
        "permission",
        [{"permId": "p", "tool": "Write", "turn_id": "t",
          "input": {"file_path": pathlib.Path("/tmp/x"), "at": _dt.datetime(2026, 1, 2, 3, 4)},
          "items": None}], [])
    assert content["input"] == {"file_path": "/tmp/x", "at": "2026-01-02 03:04:00"}
    assert "truncated" not in content

    async def run():
        orch = await _build_orch(tmp_path)
        try:
            orch._registry._pending["p1"] = _card(
                "p1", "Write", {"file_path": pathlib.Path("/tmp/x")})
            await orch._persist_status("s1", "waiting_input")
            return await _rows()
        finally:
            await _teardown(orch)

    rows = asyncio.run(run())
    assert len(rows) == 1 and rows[0].content["input"] == {"file_path": "/tmp/x"}


# ---------------------------------------------------------------------------
# ownership (#852): the primary loads every host's rows but owns only its own
# ---------------------------------------------------------------------------

async def _build_primary_with_foreign_row(tmp_path, *, row_host: str,
                                          enabled_hosts: tuple[str, ...]) -> SessionOrchestrator:
    from server.db.models import Host
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'fleet.db'}"
    await init_db(db_url=db_url)
    async with db_session() as db:
        for name in enabled_hosts:
            db.add(Host(name=name, endpoint=f"unix:///run/{name}.sock", enabled=True))
        db.add(DbSession(id="s1", name="New Session", account_name="acct1",
                         engine_session_id="claude-s1", status="waiting_question",
                         cwd="/repo/s1", host=row_host))
    assert await holds.open_hold("s1", "question", {"questions": [], "turnId": "t"}) is not None
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    return orch


def test_primary_leaves_a_live_workers_hold_alone(tmp_path, monkeypatch):
    """A row stamped for another ENABLED host has a live daemon still holding the
    ask; a primary-only restart must not announce it lost or stamp it resolved."""
    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)

    async def run():
        orch = await _build_primary_with_foreign_row(
            tmp_path, row_host="other-runner", enabled_hosts=("other-runner",))
        try:
            rec = orch.record("s1")
            return rec.lost_hold, await _rows(), await provenance.list_events("s1")
        finally:
            await _teardown(orch)

    lost, rows, events = asyncio.run(run())
    assert lost is None
    assert len(rows) == 1 and rows[0].resolved_at is None
    assert events == []


def test_primary_adopts_the_hold_of_a_host_that_left_the_registry(tmp_path, monkeypatch):
    """A stamp no longer in the registry is an orphan the primary adopts — and its
    ask really did die with that host, so it IS reported lost."""
    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)

    async def run():
        orch = await _build_primary_with_foreign_row(
            tmp_path, row_host="left-the-fleet", enabled_hosts=("other-runner",))
        try:
            rec = orch.record("s1")
            return rec.lost_hold, await _rows(), await provenance.list_events("s1")
        finally:
            await _teardown(orch)

    lost, rows, events = asyncio.run(run())
    assert lost is not None and lost["kind"] == "question"
    assert len(rows) == 1 and rows[0].resolved_at is not None
    assert [e["kind"] for e in events] == [provenance.HOLD_LOST]


def test_persist_status_survives_a_hold_snapshot_failure(tmp_path, caplog):
    """A broken driver stub must not break the turn path: the status still lands."""
    async def run():
        orch = await _build_orch(tmp_path)
        try:
            orch._drivers["s1"] = types.SimpleNamespace(
                last_hand_back_reason=None,
                pending_questions=lambda: (_ for _ in ()).throw(RuntimeError("stub")))
            with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
                final = await orch._persist_status("s1", "waiting_question")
            async with db_session() as db:
                row = (await db.execute(
                    sa_select(DbSession).where(DbSession.id == "s1"))).scalar_one()
            return final, row.status, await _rows()
        finally:
            await _teardown(orch)

    final, status, rows = asyncio.run(run())
    assert final == "waiting_question" and status == "waiting_question"
    assert rows == []
    assert any("holds.track_failed" in r.getMessage() for r in caplog.records)
