"""The hygiene executor (``needs_you_hygiene.apply_action``) and its single writers:
close comments first, board moves never add, every attempt is audited; pending
recommendations are deduped and superseded."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from server.automations import needs_you_hygiene as nyh
from server.pm.hygiene import HygieneAction

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


class _Seams:
    def __init__(self, result=None) -> None:
        self.calls: list[tuple] = []
        self.result = result or {"ok": True}

    async def pm_issue_close(self, work_item_id, close):
        self.calls.append(("close", work_item_id, close))
        return self.result

    async def pm_board_status(self, work_item_id, request):
        self.calls.append(("status", work_item_id, request))
        return self.result

    async def pm_remove_dependency(self, work_item_id, blocker):
        self.calls.append(("remove", work_item_id, blocker))
        return self.result


def _close_action() -> HygieneAction:
    return HygieneAction("done_but_open", "p", "o/r#7", "wi7", "close", {"title": "t"},
                         "the board has shown o/r#7 as Done since 2026-10-12 09:00 UTC")


async def _audits(type_: str) -> list:
    from server.db import db_session
    from server.db.models import AuditEvent
    async with db_session() as db:
        return [(a.identity, a.via, a.detail) for a in (await db.execute(
            select(AuditEvent).where(AuditEvent.type == type_))).scalars().all()]


def test_close_comments_first_then_closes_and_audits(isolated_db, monkeypatch):
    from server.github import ci_issues
    posted: list[tuple] = []

    def comment(repo, number, body, *, automation=True, **_):
        posted.append((repo, number, body, automation))
        return "https://github.com/o/r/issues/7#c1"
    monkeypatch.setattr(ci_issues, "comment_on_issue", comment)
    seams = _Seams()
    out = asyncio.run(nyh.apply_action(_close_action(), seams=seams, identity="system",
                                       actor=nyh.HYGIENE_ACTOR, via="automation"))
    assert out["ok"] and out["commentUrl"].endswith("#c1")
    assert posted == [("o/r", 7, nyh.close_comment(_close_action()), True)]
    assert "Done since" in posted[0][2]
    assert seams.calls == [("close", "wi7", {"reason": "completed"})]
    [(identity, via, detail)] = asyncio.run(_audits("pm.hygiene.done_but_open"))
    assert (identity, via, detail["ok"], detail["ref"]) == ("system", "automation", True,
                                                           "o/r#7")


def test_a_rejected_comment_never_closes(isolated_db, monkeypatch):
    from server.github import ci_issues
    monkeypatch.setattr(ci_issues, "comment_on_issue", lambda *a, **k: None)
    seams = _Seams()
    out = asyncio.run(nyh.apply_action(_close_action(), seams=seams, identity="system",
                                       actor=nyh.HYGIENE_ACTOR, via="automation"))
    assert (out["ok"], out["step"]) == (False, "comment") and seams.calls == []
    [(_, _, detail)] = asyncio.run(_audits("pm.hygiene.done_but_open"))
    assert (detail["ok"], detail["step"]) == (False, "comment")


def test_board_move_never_adds_to_a_board_and_reports_a_refusal(isolated_db):
    seams = _Seams({"ok": False, "step": "refused", "error": "held"})
    action = HygieneAction("board_blocked_no_blockers", "p", "o/r#8", "wi8", "set_status",
                           {"status": "Ready"}, "e")
    out = asyncio.run(nyh.apply_action(action, seams=seams, identity="system",
                                       actor=nyh.HYGIENE_ACTOR, via="automation"))
    assert out["step"] == "refused"
    assert seams.calls == [("status", "wi8", {"status": "Ready", "add_if_missing": False})]


def test_release_as_the_loop_ends_only_a_system_hold(isolated_db):
    from server.db import db_session
    from server.db.models import WorkItem

    async def run():
        async with db_session() as db:
            db.add_all([
                WorkItem(id="wa", repo="o/r", number=1, kind="issue", state="open",
                         hold_kind="owner_action", hold_by="reconciler", hold_reason="r"),
                WorkItem(id="wb", repo="o/r", number=2, kind="issue", state="open",
                         hold_kind="owner_action", hold_by="tony@example.com",
                         hold_reason="mine")])
        outs = []
        for n, wid in ((1, "wa"), (2, "wb")):
            outs.append(await nyh.apply_action(
                HygieneAction("stale_holds", "p", f"o/r#{n}", wid, "release_hold"),
                seams=_Seams(), identity="system", actor=nyh.HYGIENE_ACTOR,
                via="automation"))
        async with db_session() as db:
            holds = dict((await db.execute(select(WorkItem.id, WorkItem.hold_kind))).all())
        return outs, holds
    outs, holds = asyncio.run(run())
    assert [(o["ok"], o.get("step")) for o in outs] == [(True, None), (False, "not_held")]
    assert holds == {"wa": None, "wb": "owner_action"}
    [(identity, via, detail)] = asyncio.run(_audits("pm.hold.release"))
    assert (identity, via, detail["actorClass"]) == (nyh.HYGIENE_ACTOR, "automation",
                                                     "system")


def test_recommendations_dedupe_while_pending_and_supersede_when_gone(isolated_db):
    from server.db import db_session
    from server.db.models import OwnerRecommendation
    a = HygieneAction("done_but_open", "p", "o/r#7", "wi7", "close", {}, "first")
    b = HygieneAction("board_blocked_no_blockers", "p", "o/r#8", "wi8", "set_status",
                      {"status": "Ready"}, "e")

    async def run():
        async with db_session() as db:
            created = [await nyh.upsert_recommendation(db, "pid", a),
                       await nyh.upsert_recommendation(db, "pid", b),
                       await nyh.upsert_recommendation(
                           db, "pid", HygieneAction(**{**a.__dict__, "evidence": "fresher"}))]
        async with db_session() as db:
            n = await nyh.supersede_recommendations(db, "pid", {a.key}, now=NOW)
        async with db_session() as db:
            rows = (await db.execute(select(OwnerRecommendation)
                                     .order_by(OwnerRecommendation.target_ref))).scalars().all()
        return created, n, [(r.target_ref, r.status, r.evidence) for r in rows]
    created, n, rows = asyncio.run(run())
    assert created == [True, True, False] and n == 1
    assert rows == [("o/r#7", "pending", "fresher"), ("o/r#8", "superseded", "e")]


@pytest.mark.parametrize("action", ["bogus"])
def test_an_unknown_action_is_a_refusal_not_a_crash(isolated_db, action):
    out = asyncio.run(nyh.apply_action(
        HygieneAction("done_but_open", "p", "o/r#1", "w", action), seams=_Seams(),
        identity="system", actor=nyh.HYGIENE_ACTOR, via="automation"))
    assert (out["ok"], out["step"]) == (False, "unknown_action")
