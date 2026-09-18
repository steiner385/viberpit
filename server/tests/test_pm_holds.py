"""Pure hold rules (spec 2026-09-18 §3, §8): kinds, actor classes, permissions."""
from datetime import datetime, timezone

import pytest

from server.db.models import HOLD_KINDS, WORK_ITEM_TRIAGE_STATES, WorkItem
from server.pm.holds import (
    AGENT_SETTABLE, HoldPermissionError, actor_class, clear_hold, may_set, set_hold,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def test_vocabularies():
    assert HOLD_KINDS == ("evidence", "stronger_model", "decision", "owner_action")
    assert "needs_human" not in WORK_ITEM_TRIAGE_STATES
    assert AGENT_SETTABLE == frozenset({"evidence", "stronger_model"})


@pytest.mark.parametrize("actor,cls", [
    ("tony@example.com", "person"),
    ("session:abc-123", "agent"),
    ("assistant:tony@example.com", "agent"),
    ("lan@local", "agent"),
    ("runner", "system"),
    ("automation:error-triage", "system"),
    ("migration", "system"),
])
def test_actor_class(actor, cls):
    assert actor_class(actor) == cls


def test_may_set_by_class():
    person, agent, system = "tony@example.com", "session:s1", "runner"
    for kind in HOLD_KINDS:
        assert may_set(person, kind)
    assert may_set(agent, "evidence") and may_set(agent, "stronger_model")
    assert not may_set(agent, "decision") and not may_set(agent, "owner_action")
    # system actors (runner, automations) are the ladder and the reject flows: any kind.
    for kind in HOLD_KINDS:
        assert may_set(system, kind)


def test_set_hold_records_everything_and_refuses_bad_input():
    wi = WorkItem(repo="o/r", number=1, kind="issue", body="hello")
    set_hold(wi, kind="evidence", reason="profile the lag", actor="session:s1", now=NOW)
    assert wi.hold_kind == "evidence"
    assert wi.hold_reason == "profile the lag"
    assert wi.hold_by == "session:s1"
    assert wi.hold_set_at == NOW
    assert wi.hold_fingerprint  # body fingerprint captured
    with pytest.raises(HoldPermissionError):
        set_hold(wi, kind="owner_action", reason="x", actor="session:s1", now=NOW)
    with pytest.raises(ValueError):
        set_hold(wi, kind="evidence", reason="  ", actor="tony@example.com", now=NOW)
    with pytest.raises(ValueError):
        set_hold(wi, kind="bogus", reason="r", actor="tony@example.com", now=NOW)


def test_set_hold_may_not_overwrite_a_hold_the_actor_could_not_clear():
    """Replacing a hold RELEASES the old one, so ``set_hold`` must gate on the kind
    already on the row as well as the new kind (final-review F1). Otherwise an agent
    setting ``evidence`` over a person's ``owner_action`` silently un-parks the item
    and the runner dispatches work a person explicitly stopped."""
    wi = WorkItem(repo="o/r", number=1, kind="issue", body="hello")
    set_hold(wi, kind="owner_action", reason="owner only", actor="tony@example.com", now=NOW)
    with pytest.raises(HoldPermissionError):
        set_hold(wi, kind="evidence", reason="correlate", actor="session:s1", now=NOW)
    assert (wi.hold_kind, wi.hold_by) == ("owner_action", "tony@example.com")
    # a person may replace it
    set_hold(wi, kind="evidence", reason="get a repro", actor="tony@example.com", now=NOW)
    assert (wi.hold_kind, wi.hold_by) == ("evidence", "tony@example.com")
    # and an agent may replace an agent-settable kind with another
    set_hold(wi, kind="stronger_model", reason="under-powered", actor="session:s1", now=NOW)
    assert (wi.hold_kind, wi.hold_by) == ("stronger_model", "session:s1")
    # re-setting the SAME kind is not a replacement — an agent may refresh its own reason
    set_hold(wi, kind="stronger_model", reason="still under-powered", actor="session:s1", now=NOW)
    assert wi.hold_reason == "still under-powered"


def test_clear_hold_limited_to_settable_kinds():
    wi = WorkItem(repo="o/r", number=1, kind="issue", body="hello")
    set_hold(wi, kind="owner_action", reason="owner only", actor="tony@example.com", now=NOW)
    with pytest.raises(HoldPermissionError):
        clear_hold(wi, actor="session:s1")
    clear_hold(wi, actor="tony@example.com")
    assert wi.hold_kind is None and wi.hold_reason is None and wi.hold_by is None
    assert wi.hold_set_at is None and wi.hold_fingerprint is None
    # clearing nothing is a no-op for anyone
    clear_hold(wi, actor="session:s1")


def test_set_reject_hold_is_open_to_every_actor_class():
    """Spec §8 authorises ``owner_action`` from "the two reject flows" whoever drives
    them, so the reject helper writes the same fields as ``set_hold`` but takes no
    actor-class gate — otherwise the inbox Reject button 422s in LAN-open mode and an
    assistant rejection cannot be recorded at all (final-review F4)."""
    from server.pm.holds import set_reject_hold
    wi = WorkItem(repo="o/r", number=1, kind="issue", body="hello")
    set_reject_hold(wi, actor="lan@local", now=NOW)
    assert wi.hold_kind == "owner_action"
    assert wi.hold_by == "lan@local"
    assert wi.hold_reason == "classification proposal rejected"
    assert wi.hold_set_at == NOW
    assert wi.hold_fingerprint
    # and it may park an item an agent had merely held for evidence
    set_hold(wi, kind="evidence", reason="repro", actor="tony@example.com", now=NOW)
    set_reject_hold(wi, actor="assistant:tony@example.com", now=NOW)
    assert (wi.hold_kind, wi.hold_by) == ("owner_action", "assistant:tony@example.com")


def test_actor_for_request_reads_via_and_identity():
    from types import SimpleNamespace
    from server.pm.holds import actor_for_request
    person = SimpleNamespace(state=SimpleNamespace(identity="tony@example.com", via=None))
    assert actor_for_request(person) == "tony@example.com"
    drawer = SimpleNamespace(state=SimpleNamespace(identity="tony@example.com", via="assistant"))
    assert actor_for_request(drawer) == "assistant:tony@example.com"
    lan = SimpleNamespace(state=SimpleNamespace(identity="lan@local", via=None))
    assert actor_for_request(lan) == "lan@local"


def test_apply_session_hold_resolves_ref_and_limits_kind(tmp_path):
    import asyncio
    from server.db import db_session, init_db
    from server.pm.holds import apply_session_hold

    async def _run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
        async with db_session() as db:
            db.add(WorkItem(id="w1", repo="o/r", number=7, kind="issue", body="b",
                            triage_state="classified"))
            await db.flush()
            wi, released = await apply_session_hold(db, ref="o/r#7", kind="evidence",
                                                    reason="needs a repro", session_id="s1", now=NOW)
            assert (wi.hold_kind, wi.hold_by) == ("evidence", "session:s1")
            assert released is None  # setting, not releasing — nothing was cleared
            with pytest.raises(HoldPermissionError):
                await apply_session_hold(db, ref="o/r#7", kind="owner_action",
                                         reason="park", session_id="s1", now=NOW)
            wi, released = await apply_session_hold(db, ref="o/r#7", kind=None, reason="found it",
                                                     session_id="s1", now=NOW)
            assert wi.hold_kind is None
            assert released == "evidence"  # what WAS held before this clear
            wi, released = await apply_session_hold(db, ref="o/r#7", kind=None, reason="still none",
                                                     session_id="s1", now=NOW)
            assert released is None  # already unheld — a true no-op clear
            with pytest.raises(ValueError):
                await apply_session_hold(db, ref="o/r#99", kind="evidence", reason="r",
                                         session_id="s1", now=NOW)
    asyncio.run(_run())


def test_release_via_api_is_the_one_release_and_audit_sequence(tmp_path):
    """ONE helper behind both API taps (Retry and Implement now): the decision
    refusal, the permission gate, the clear, and the ``via="api"`` audit."""
    import asyncio
    from sqlalchemy import select
    from server.db import db_session, init_db
    from server.db.models import AuditEvent
    from server.pm.holds import DecisionHoldError, release_via_api

    async def _run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
        async with db_session() as db:
            def add(item_id, kind):
                wi = WorkItem(id=item_id, repo="o/r", number=int(item_id[1:]), kind="issue",
                              title="t", body="b")
                set_hold(wi, kind=kind, reason="r", actor="tony@example.com", now=NOW)
                db.add(wi)
                return wi
            owner, decision, evidence = add("w1", "owner_action"), add("w2", "decision"), add("w3", "evidence")
            unheld = WorkItem(id="w4", repo="o/r", number=4, kind="issue", title="t", body="b")
            db.add(unheld)
            await db.flush()

            released = await release_via_api(db, owner, actor="tony@example.com",
                                             identity="tony@example.com", signal="retry")
            assert released == "owner_action" and owner.hold_kind is None

            # A decision hold is refused before anything is cleared (spec §8).
            with pytest.raises(DecisionHoldError):
                await release_via_api(db, decision, actor="tony@example.com",
                                      identity="tony@example.com", signal="retry")
            assert decision.hold_kind == "decision"

            # `only_kind` is how the dispatch tap leaves the kinds it has no
            # release path for alone, without failing the dispatch.
            assert await release_via_api(db, evidence, actor="tony@example.com",
                                         identity="tony@example.com", signal="implement",
                                         only_kind="owner_action") is None
            assert evidence.hold_kind == "evidence"

            # Unheld: a no-op that audits nothing.
            assert await release_via_api(db, unheld, actor="tony@example.com",
                                         identity="tony@example.com", signal="retry") is None

            # An agent-class actor may not clear an owner_action hold.
            agent_held = add("w5", "owner_action")
            await db.flush()
            with pytest.raises(HoldPermissionError):
                await release_via_api(db, agent_held, actor="lan@local",
                                      identity="lan@local", signal="implement",
                                      only_kind="owner_action")
            assert agent_held.hold_kind == "owner_action"

            rows = (await db.execute(select(AuditEvent).where(
                AuditEvent.type == "pm.hold.release"))).scalars().all()
            assert [(r.detail["workItemId"], r.detail["kind"], r.detail["actorClass"],
                     r.detail["signal"], r.via) for r in rows] == [
                ("w1", "owner_action", "person", "retry", "api")]
    asyncio.run(_run())


def test_runner_and_reconciler_are_system_actors():
    from server.pm.holds import RECONCILER_ACTOR, RUNNER_ACTOR, actor_class, set_runner_hold
    assert actor_class(RUNNER_ACTOR) == "system"
    assert actor_class(RECONCILER_ACTOR) == "system"
    wi = WorkItem(repo="o/r", number=1, kind="issue", title="t", body="b")
    set_runner_hold(wi, reason="max attempts (2) reached with no mergeable PR", now=NOW)
    assert (wi.hold_kind, wi.hold_by) == ("owner_action", RUNNER_ACTOR)
    assert wi.hold_set_at == NOW and wi.hold_fingerprint


def test_set_runner_stronger_hold_sets_kind_by_and_reason():
    """Spec 2026-09-18 §4: a ladder rung climb parks the issue ``stronger_model``
    (agent-settable, queue-eligible), as the runner system actor."""
    from server.pm.holds import RUNNER_ACTOR, set_runner_stronger_hold
    wi = WorkItem(repo="o/r", number=1, kind="issue", title="t", body="b")
    set_runner_stronger_hold(wi, reason="bounced at sonnet; next attempt on opus", now=NOW)
    assert (wi.hold_kind, wi.hold_by) == ("stronger_model", RUNNER_ACTOR)
    assert wi.hold_reason == "bounced at sonnet; next attempt on opus"
    assert wi.hold_set_at == NOW and wi.hold_fingerprint


def _sync_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from server.db.models import Base
    e = create_engine("sqlite://")
    Base.metadata.create_all(e)
    return Session(e)


def _held(db, number, *, kind="owner_action", body="b", state="open"):
    wi = WorkItem(repo="o/r", number=number, kind="issue", state=state, title="t", body=body)
    set_hold(wi, kind=kind, reason="r", actor="tony@example.com", now=NOW)
    db.add(wi)
    db.flush()
    return wi


def test_held_refs_lists_open_owner_action_and_decision_holds_only():
    from server.pm.holds import held_refs
    db = _sync_db()
    _held(db, 1)
    _held(db, 2, kind="decision")
    _held(db, 3, kind="evidence")
    _held(db, 4, state="closed")
    db.add(WorkItem(repo="o/r", number=5, kind="issue", state="open", title="t", body="b"))
    db.commit()
    assert held_refs(db) == frozenset({"o/r#1", "o/r#2"})


def test_release_by_signals_board_edit_and_body_edit():
    from server.pm.holds import RELEASE_SIGNALS, release_by_signals
    from server.pm.mirror_store import HumanEdit
    db = _sync_db()
    by_board = _held(db, 1)
    by_body = _held(db, 2)
    untouched = _held(db, 3)
    evidence = _held(db, 4, kind="evidence")
    by_body.body = "edited after the hold"          # fingerprint drift
    db.flush()
    edits = [HumanEdit("o/r#1", "Status", "Needs human", "Ready"),
             HumanEdit("o/r#3", "Score", 1.0, 2.0),           # not a status edit
             HumanEdit("o/r#4", "Status", "Needs human", "Backlog")]  # evidence: not released
    released = release_by_signals(db, human_edits=edits, now=NOW)
    db.commit()
    assert sorted((w.number, s) for w, s in released) == [(1, "board_edit"), (2, "body_edit")]
    assert by_board.hold_kind is None and by_body.hold_kind is None
    assert untouched.hold_kind == "owner_action"
    assert evidence.hold_kind == "evidence"
    # Every way an owner_action hold ends, in ONE tuple: the two this rule detects,
    # the Retry endpoint, and an explicit clear through the overlay editor. Time is
    # not one of them.
    assert set(RELEASE_SIGNALS) == {"board_edit", "body_edit", "retry", "overlay", "session", "implement"}
    from server.db.models import AuditEvent
    rows = db.query(AuditEvent).filter(AuditEvent.type == "pm.hold.release").all()
    assert sorted(r.detail["signal"] for r in rows) == ["board_edit", "body_edit"]
    assert all(r.detail["actorClass"] == "system" and r.detail["kind"] == "owner_action" for r in rows)
    # idempotent: a second pass releases nothing
    assert release_by_signals(db, human_edits=edits, now=NOW) == []
