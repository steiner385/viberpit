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


# --- close-out assessment outcomes (design §3) ---------------------------------------------

def _closeout_reason() -> str:
    from server.automations.backlog_github import closeout_hold_reason
    return closeout_hold_reason("the PR references it with `Refs`")


async def _seed_closeout(number: int, *, hold_by: str = "runner", reason: str | None = None):
    from server.automations.context import AutomationContext
    from server.db import db_session
    from server.db.models import WorkItem
    async with db_session() as db:
        db.add(WorkItem(id=f"wi{number}", repo="o/r", number=number, kind="issue",
                        state="open", title="t", body="b", hold_kind="owner_action",
                        hold_by=hold_by, hold_reason=reason or _closeout_reason(),
                        hold_set_at=NOW.replace(tzinfo=None)))
    await AutomationContext(nyh.AUTOMATION_ID).set_target_state(
        f"o/r#{number}", {"fingerprint": "f", "decision": "x", "appliedAt": None})


def _closeout_action(number: int, action: str, **detail) -> HygieneAction:
    return HygieneAction("closeout_assess", "p", f"o/r#{number}", f"wi{number}", action,
                         {"holdReason": _closeout_reason(), **detail}, "assessed")


async def _hold(number: int):
    from server.db import db_session
    from server.db.models import WorkItem
    async with db_session() as db:
        wi = await db.get(WorkItem, f"wi{number}")
        assert wi is not None
        return (wi.hold_kind, wi.hold_reason, wi.hold_by)


async def _applied_at(number: int):
    from server.automations.context import AutomationContext
    return (await AutomationContext(nyh.AUTOMATION_ID).get_target_state(
        f"o/r#{number}")).get("appliedAt")


def test_closeout_rewrite_narrows_the_hold_audits_and_stamps_the_assessment(isolated_db):
    async def run():
        await _seed_closeout(1)
        out = await nyh.apply_action(
            _closeout_action(1, "rewrite_hold", reason="Left for you:\n- [ ] prod check"),
            seams=_Seams(), identity="tony@example.com", actor="tony@example.com", via="api")
        return out, await _hold(1), await _applied_at(1)
    out, hold, applied = asyncio.run(run())
    assert out["ok"]
    # The words are the automation's whoever applied them; the audit names the applier.
    assert hold == ("owner_action", "Left for you:\n- [ ] prod check", nyh.HYGIENE_ACTOR)
    assert applied is not None
    [(identity, via, detail)] = asyncio.run(_audits("pm.hygiene.closeout_assess"))
    assert (identity, via, detail["action"], detail["ok"]) == (
        "tony@example.com", "api", "rewrite_hold", True)


def test_closeout_release_ends_the_hold_as_assessment(isolated_db):
    async def run():
        await _seed_closeout(2)
        out = await nyh.apply_action(_closeout_action(2, "release_hold", note="add a test"),
                                     seams=_Seams(), identity="system",
                                     actor=nyh.HYGIENE_ACTOR, via="automation")
        return out, await _hold(2), await _applied_at(2)
    out, hold, applied = asyncio.run(run())
    assert out["ok"] and hold == (None, None, None) and applied is not None
    [(identity, via, detail)] = asyncio.run(_audits("pm.hold.release"))
    assert (identity, via, detail["signal"]) == (nyh.HYGIENE_ACTOR, "automation", "assessment")


def test_closeout_close_posts_the_evidence_comment(isolated_db, monkeypatch):
    from server.github import ci_issues
    posted: list[str] = []
    monkeypatch.setattr(ci_issues, "comment_on_issue",
                        lambda repo, number, body, **_: posted.append(body) or "url")
    seams = _Seams()

    async def run():
        await _seed_closeout(3)
        return await nyh.apply_action(
            _closeout_action(3, "close", comment="The merged PR(s) already cover: - a — x.py:3"),
            seams=seams, identity="system", actor=nyh.HYGIENE_ACTOR, via="automation")
    assert asyncio.run(run())["ok"]
    assert posted[0].startswith("The merged PR(s) already cover: - a — x.py:3")
    assert seams.calls == [("close", "wi3", {"reason": "completed"})]


@pytest.mark.parametrize("action", ["close", "release_hold", "rewrite_hold"])
def test_a_closeout_outcome_never_touches_a_hold_that_changed(isolated_db, monkeypatch, action):
    """A person re-parked it (or re-worded it) after the assessment: nothing is
    written, nothing stamped, and the refusal is audited."""
    from server.github import ci_issues
    monkeypatch.setattr(ci_issues, "comment_on_issue", lambda *a, **k: "url")
    seams = _Seams()

    async def run():
        await _seed_closeout(4, hold_by="tony@example.com")
        out = await nyh.apply_action(_closeout_action(4, action, reason="Left for you: x"),
                                     seams=seams, identity="system",
                                     actor=nyh.HYGIENE_ACTOR, via="automation")
        return out, await _hold(4), await _applied_at(4)
    out, hold, applied = asyncio.run(run())
    assert (out["ok"], out["step"]) == (False, "hold_changed")
    assert hold == ("owner_action", _closeout_reason(), "tony@example.com")
    assert applied is None and seams.calls == []


# --- parked review outcomes (design §4) ------------------------------------------------------

async def _seed_parked(number: int, *, labels=("Parked", "bug"), state: str = "open"):
    from server.automations.context import AutomationContext
    from server.db import db_session
    from server.db.models import WorkItem
    from server.pm.parked_review import item_target
    async with db_session() as db:
        db.add(WorkItem(id=f"wi{number}", repo="o/r", number=number, kind="issue",
                        state=state, title="t", body="b", labels=list(labels)))
    await AutomationContext(nyh.AUTOMATION_ID).set_target_state(
        item_target(f"o/r#{number}"), {"fingerprint": "f", "verdict": "unpark",
                                       "appliedAt": None})


async def _parked_applied_at(number: int):
    from server.automations.context import AutomationContext
    from server.pm.parked_review import item_target
    return (await AutomationContext(nyh.AUTOMATION_ID).get_target_state(
        item_target(f"o/r#{number}"))).get("appliedAt")


def _parked_action(number: int, action: str, **detail) -> HygieneAction:
    return HygieneAction("parked_review", "p", f"o/r#{number}", f"wi{number}", action,
                         {"title": "t", "summary": "still wanted", **detail}, "reviewed")


def _fake_gh(monkeypatch, *, remove_ok: bool = True):
    from server.github import ci_issues
    calls: list[tuple] = []

    def comment(repo, number, body, **kw):
        calls.append(("comment", repo, number, body, kw.get("automation")))
        return "url"

    def remove_label(repo, number, name, **_):
        calls.append(("unlabel", repo, number, name))
        return remove_ok
    monkeypatch.setattr(ci_issues, "comment_on_issue", comment)
    monkeypatch.setattr(ci_issues, "remove_label", remove_label)
    return calls


def test_an_approved_unpark_explains_then_removes_the_label_as_spelled(isolated_db, monkeypatch):
    calls = _fake_gh(monkeypatch)

    async def run():
        await _seed_parked(11)
        out = await nyh.apply_action(_parked_action(11, "unpark", label="Parked"),
                                     seams=_Seams(), identity="tony@example.com",
                                     actor="tony@example.com", via="api")
        return out, await _parked_applied_at(11)
    out, applied = asyncio.run(run())
    assert out["ok"] and applied is not None
    assert [c[0] for c in calls] == ["comment", "unlabel"]
    assert calls[0][4] is True and "still wanted" in calls[0][3]
    assert calls[1] == ("unlabel", "o/r", 11, "Parked")
    [(identity, via, detail)] = asyncio.run(_audits("pm.hygiene.parked_review"))
    assert (identity, via, detail["action"], detail["ok"]) == (
        "tony@example.com", "api", "unpark", True)


async def _seed_evidence(number: int, *, hold_kind: str = "owner_evidence",
                         asked: datetime = NOW, replied: bool = True) -> str:
    from server.db import db_session
    from server.db.models import WorkItem
    async with db_session() as db:
        wi = WorkItem(repo="o/r", number=number, kind="issue", title=f"issue {number}",
                      state="open", body="b", url=f"https://x/{number}", author="tony",
                      hold_kind=hold_kind, hold_by="runner", hold_reason="needs owner record",
                      hold_set_at=asked, hold_detail={"request": "Record the DPA dates"},
                      last_human_comment_at=asked.replace(hour=13) if replied else None)
        db.add(wi)
        await db.commit()
        return wi.id


def _evidence_action(number: int, wid: str) -> HygieneAction:
    return HygieneAction("evidence_posted", "p", f"o/r#{number}", wid, "close",
                         {"title": "t", "holdSetAt": NOW.isoformat(), "closeReason": "completed",
                          "comment": "The evidence asked for on 2026-10-14 was posted here."},
                         f"o/r#{number} waited for evidence")


def test_an_approved_evidence_close_comments_closes_and_ends_the_ask(isolated_db, monkeypatch):
    """#3113 (after #3128): the owner's tap closes the issue through the ordinary close
    path, then ends the ``owner_evidence`` hold it answered, audited
    ``evidence_supplied``."""
    from server.db import db_session
    from server.db.models import AuditEvent, WorkItem
    calls = _fake_gh(monkeypatch)
    seams = _Seams()

    async def run():
        wid = await _seed_evidence(31)
        out = await nyh.apply_action(_evidence_action(31, wid), seams=seams,
                                     identity="tony@example.com", actor="tony@example.com",
                                     via="api")
        async with db_session() as db:
            wi = await db.get(WorkItem, wid)
            audits = (await db.execute(select(AuditEvent).where(
                AuditEvent.type == "pm.hold.release"))).scalars().all()
            return out, wid, wi.hold_kind, [a.detail["signal"] for a in audits]
    out, wid, kind, signals = asyncio.run(run())
    assert out["ok"] is True
    assert [c[:3] for c in calls] == [("comment", "o/r", 31)]
    assert "was posted here" in calls[0][3] and "Closing as completed" in calls[0][3]
    assert seams.calls == [("close", wid, {"reason": "completed"})]
    assert kind is None and signals == ["evidence_supplied"]


def test_the_loop_can_never_apply_an_evidence_close(isolated_db, monkeypatch):
    """Recommend-only (#3113): the mirror does not know WHO commented."""
    calls = _fake_gh(monkeypatch)
    seams = _Seams()

    async def run():
        wid = await _seed_evidence(32)
        return await nyh.apply_action(_evidence_action(32, wid), seams=seams,
                                      identity="system", actor=nyh.HYGIENE_ACTOR,
                                      via="automation")
    out = asyncio.run(run())
    assert (out["ok"], out["step"]) == (False, "recommend_only")
    assert calls == [] and seams.calls == []


@pytest.mark.parametrize("over", [{"asked": NOW.replace(hour=11)}, {"replied": False},
                                  {"hold_kind": "owner_action"}])
def test_an_evidence_close_never_touches_an_ask_that_changed(isolated_db, monkeypatch, over):
    calls = _fake_gh(monkeypatch)
    seams = _Seams()

    async def run():
        wid = await _seed_evidence(33, **over)
        return await nyh.apply_action(_evidence_action(33, wid), seams=seams,
                                      identity="tony@example.com", actor="tony@example.com",
                                      via="api")
    out = asyncio.run(run())
    assert (out["ok"], out["step"]) == (False, "evidence_hold_moved")
    assert calls == [] and seams.calls == []


def test_the_loop_can_never_apply_a_parked_review(isolated_db, monkeypatch):
    """Recommend-only (design §4: no automatic unpark, ever) — even if a caller
    hands the loop's executor a parked-review action."""
    calls = _fake_gh(monkeypatch)
    seams = _Seams()

    async def run():
        await _seed_parked(12)
        return await nyh.apply_action(_parked_action(12, "unpark", label="Parked"),
                                      seams=seams, identity="system",
                                      actor=nyh.HYGIENE_ACTOR, via="automation")
    out = asyncio.run(run())
    assert (out["ok"], out["step"]) == (False, "recommend_only")
    assert calls == [] and seams.calls == []


@pytest.mark.parametrize("labels,state", [(("bug",), "open"), (("parked",), "closed")])
def test_a_parked_review_never_touches_an_issue_no_longer_parked(isolated_db, monkeypatch,
                                                                 labels, state):
    calls = _fake_gh(monkeypatch)
    seams = _Seams()

    async def run():
        await _seed_parked(13, labels=labels, state=state)
        out = await nyh.apply_action(_parked_action(13, "close", closeReason="not_planned",
                                                    comment="gone"),
                                     seams=seams, identity="tony@example.com",
                                     actor="tony@example.com", via="api")
        return out, await _parked_applied_at(13)
    out, applied = asyncio.run(run())
    assert (out["ok"], out["step"]) == (False, "not_parked")
    assert calls == [] and seams.calls == [] and applied is None


def test_an_obsolete_parked_issue_closes_as_not_planned(isolated_db, monkeypatch):
    calls = _fake_gh(monkeypatch)
    seams = _Seams()

    async def run():
        await _seed_parked(14)
        return await nyh.apply_action(
            _parked_action(14, "close", closeReason="not_planned",
                           comment="This parked issue no longer applies.\n\nx.py was removed"),
            seams=seams, identity="tony@example.com", actor="tony@example.com", via="api")
    assert asyncio.run(run())["ok"]
    assert "Closing as not planned." in calls[0][3]
    assert seams.calls == [("close", "wi14", {"reason": "not_planned"})]


def test_a_refused_label_removal_is_a_failure_not_a_stamp(isolated_db, monkeypatch):
    _fake_gh(monkeypatch, remove_ok=False)

    async def run():
        await _seed_parked(15)
        out = await nyh.apply_action(_parked_action(15, "unpark", label="parked"),
                                     seams=_Seams(), identity="tony@example.com",
                                     actor="tony@example.com", via="api")
        return out, await _parked_applied_at(15)
    out, applied = asyncio.run(run())
    assert (out["ok"], out["step"]) == (False, "label") and applied is None
