"""Pure hold rules (spec 2026-09-18 §3, §8): kinds, actor classes, permissions."""
from datetime import datetime, timezone

import pytest

from server.db.models import HOLD_KINDS, WORK_ITEM_TRIAGE_STATES, WorkItem
from server.pm.holds import (
    AGENT_SETTABLE, HoldPermissionError, actor_class, clear_hold, may_set, set_decision_hold,
    set_hold,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _park(wi, kind, *, actor="tony@example.com"):
    """Put *wi* on a hold of *kind* the way the production code does. A
    ``decision`` hold has exactly ONE writer (spec §8) and ``set_hold`` refuses the
    kind outright, so a fixture that wants one goes through ``set_decision_hold``."""
    if kind == "decision":
        set_decision_hold(wi, reason="r", actor=actor, now=NOW)
    else:
        set_hold(wi, kind=kind, reason="r", actor=actor, now=NOW)


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
                _park(wi, kind)
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
    _park(wi, kind)
    db.add(wi)
    db.flush()
    return wi


def test_held_refs_lists_open_owner_action_and_decision_holds_only():
    """And it names the KIND, not just the ref: the runner's give-up sweep only needs
    membership, but ``_settle_decision`` must tell a ``decision`` park from an
    ``owner_action`` one — a person's park is not proof a proposal landed. Both
    questions ride this single read."""
    from server.pm.holds import held_refs
    db = _sync_db()
    _held(db, 1)
    _held(db, 2, kind="decision")
    _held(db, 3, kind="evidence")
    _held(db, 4, state="closed")
    db.add(WorkItem(repo="o/r", number=5, kind="issue", state="open", title="t", body="b"))
    db.commit()
    assert held_refs(db) == {"o/r#1": "owner_action", "o/r#2": "decision"}


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
    # The old value is whatever the board was spelling: "Held" since the rename, and
    # "Needs human" on a board mid-rollout (§9). Neither is read — a Status edit off
    # the held column is the signal, not the value it moved from.
    edits = [HumanEdit("o/r#1", "Status", "Held", "Ready"),
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
    # not one of them — except ``expired``, which ends only a migration hold (#2682);
    # nor is a policy edit — except ``policy_skip``, which ends only a hold the
    # RUNNER wrote on an issue the product's policy now skips (KinDash#16652).
    assert set(RELEASE_SIGNALS) == {"board_edit", "body_edit", "retry", "overlay", "session",
                                    "implement", "dispatch", "investigation", "decision",
                                    "escalation", "expired", "policy_skip"}
    from server.db.models import AuditEvent
    rows = db.query(AuditEvent).filter(AuditEvent.type == "pm.hold.release").all()
    assert sorted(r.detail["signal"] for r in rows) == ["board_edit", "body_edit"]
    assert all(r.detail["actorClass"] == "system" and r.detail["kind"] == "owner_action" for r in rows)
    # idempotent: a second pass releases nothing
    assert release_by_signals(db, human_edits=edits, now=NOW) == []


def test_release_expired_migration_holds_returns_only_conversion_holds_past_the_ttl():
    """#2682: the legacy conversion's "pre-ladder needs_human" holds are no decision,
    so they expire; every other owner_action hold — the runner's, a person's, a
    fresh conversion hold — is untouched, and a re-run releases nothing."""
    from datetime import timedelta

    from server.db.models import AuditEvent
    from server.pm.holds import (MIGRATION_ACTOR, MIGRATION_HOLD_TTL,
                                 release_expired_migration_holds)
    db = _sync_db()
    old = _held(db, 1)
    undated = _held(db, 2)
    fresh = _held(db, 3)
    person = _held(db, 4)
    for wi, age in ((old, MIGRATION_HOLD_TTL + timedelta(days=16)), (undated, None),
                    (fresh, timedelta(days=1))):
        wi.hold_by = MIGRATION_ACTOR
        wi.hold_reason = "pre-ladder needs_human"
        wi.hold_set_at = None if age is None else (NOW - age).replace(tzinfo=None)
    person.hold_set_at = (NOW - timedelta(days=60)).replace(tzinfo=None)
    db.commit()
    released = release_expired_migration_holds(db, now=NOW)
    db.commit()
    assert sorted((w.number, s) for w, s in released) == [(1, "expired"), (2, "expired")]
    assert old.hold_kind is None and undated.hold_kind is None
    assert fresh.hold_kind == "owner_action" and person.hold_kind == "owner_action"
    rows = db.query(AuditEvent).filter(AuditEvent.type == "pm.hold.release").all()
    assert [r.detail["signal"] for r in rows] == ["expired", "expired"]
    assert all(r.identity == "reconciler" for r in rows)
    assert release_expired_migration_holds(db, now=NOW) == []


def test_release_on_dispatch_clears_stronger_model_holds_only():
    """Spec 2026-09-18 §4: the dispatch CONSUMES the ``stronger_model`` hold it was
    climbing for — and nothing else. A person's ``owner_action`` and an agent's
    ``evidence`` hold are other people's parks; a spawn is not their release
    signal."""
    from server.pm.holds import RELEASE_SIGNALS, release_on_dispatch
    db = _sync_db()
    a = _held(db, 1, kind="stronger_model")
    b = _held(db, 2, kind="owner_action")
    c = _held(db, 3, kind="evidence")
    db.commit()
    assert "dispatch" in RELEASE_SIGNALS
    released = release_on_dispatch(db, ["o/r#1", "o/r#2", "o/r#3", "o/r#9"], now=NOW)
    db.commit()
    assert released == ["o/r#1"]
    assert a.hold_kind is None and b.hold_kind == "owner_action" and c.hold_kind == "evidence"
    from server.db.models import AuditEvent
    rows = db.query(AuditEvent).filter(AuditEvent.type == "pm.hold.release").all()
    assert [r.detail["signal"] for r in rows] == ["dispatch"]
    assert rows[0].detail["actorClass"] == "system" and rows[0].detail["kind"] == "stronger_model"
    assert rows[0].identity == "runner" and rows[0].via == "runner"
    # Idempotent, and blind to a malformed ref: the hold is gone, so a re-run of
    # the same batch releases nothing.
    assert release_on_dispatch(db, ["o/r#1", "not-a-ref"], now=NOW) == []


def test_decision_hold_is_settable_by_a_session_and_cleared_only_as_a_decision():
    """Spec 2026-09-18 §6/§8: the intended writer of a ``decision`` hold is a coding
    session — agent class, and ``decision`` is not in ``AGENT_SETTABLE`` — so the two
    decision helpers are ungated on purpose, like the reject flows. What they must NOT
    do is touch a kind that is not theirs."""
    from server.pm.holds import (DECISION_SET_MESSAGE, DecisionHoldError, clear_decision_hold,
                                 set_decision_hold)
    wi = WorkItem(repo="o/r", number=1, kind="issue", title="t", body="b")
    # The generic setter refuses the kind outright — for a person as much as for a
    # session — because a decision hold is the shadow of a proposal (spec §8).
    with pytest.raises(DecisionHoldError, match=DECISION_SET_MESSAGE):
        set_hold(wi, kind="decision", reason="r", actor="session:s1", now=NOW)
    with pytest.raises(DecisionHoldError, match=DECISION_SET_MESSAGE):
        set_hold(wi, kind="decision", reason="r", actor="tony@example.com", now=NOW)
    set_decision_hold(wi, reason="decision pending: per repo or global?",
                      actor="session:s1", now=NOW)
    assert (wi.hold_kind, wi.hold_by) == ("decision", "session:s1")
    assert wi.hold_set_at == NOW and wi.hold_fingerprint
    with pytest.raises(ValueError):
        set_decision_hold(wi, reason="  ", actor="session:s1", now=NOW)

    # the accept releases it — as an agent-class actor, which ``clear_hold`` refuses
    with pytest.raises(HoldPermissionError):
        clear_hold(wi, actor="lan@local")
    assert clear_decision_hold(wi, actor="lan@local") is True
    assert wi.hold_kind is None and wi.hold_by is None and wi.hold_fingerprint is None
    # idempotent, and narrowed to the kind it owns
    assert clear_decision_hold(wi, actor="tony@example.com") is False
    set_hold(wi, kind="owner_action", reason="mine now", actor="tony@example.com", now=NOW)
    assert clear_decision_hold(wi, actor="session:s1") is False
    assert wi.hold_kind == "owner_action"


def test_set_reject_decision_hold_parks_the_item_for_the_person_who_rejected():
    from server.pm.holds import DECISION_REJECT_REASON, set_reject_decision_hold
    wi = WorkItem(repo="o/r", number=1, kind="issue", title="t", body="b")
    set_reject_decision_hold(wi, actor="tony@example.com", now=NOW)
    assert (wi.hold_kind, wi.hold_by) == ("owner_action", "tony@example.com")
    assert wi.hold_reason == DECISION_REJECT_REASON
    assert wi.hold_set_at == NOW and wi.hold_fingerprint


@pytest.mark.asyncio
async def test_release_via_api_still_refuses_a_decision_hold():
    """The generic release taps (Retry, Implement now) must keep refusing a decision:
    it ends when the person picks an option, not on a button (spec §6)."""
    from server.pm.holds import (DECISION_HOLD_MESSAGE, DecisionHoldError, release_via_api,
                                 set_decision_hold)
    wi = WorkItem(repo="o/r", number=1, kind="issue", title="t", body="b")
    set_decision_hold(wi, reason="r", actor="tony@example.com", now=NOW)
    with pytest.raises(DecisionHoldError, match=DECISION_HOLD_MESSAGE):
        await release_via_api(None, wi, actor="tony@example.com",
                              identity="tony@example.com", signal="retry")
    assert wi.hold_kind == "decision"


def test_release_policy_skipped_holds_ends_only_system_owner_action_holds():
    """The hold-hygiene release (cairnea/KinDash#16652): among the refs it is handed
    it ends SYSTEM actors' ``owner_action`` holds only (design 2026-10-14 §2) — a
    person's park, an evidence hold and an unlisted ref are untouched — audited as
    ``policy_skip`` via the runner, and a second pass finds nothing."""
    from server.db.models import AuditEvent
    from server.pm.holds import RELEASE_SIGNALS, release_policy_skipped_holds, set_runner_hold
    db = _sync_db()
    runner = WorkItem(repo="o/r", number=1, kind="issue", state="open", title="t", body="b")
    set_runner_hold(runner, reason="bounced at opus; fable needs approval", now=NOW)
    unlisted = WorkItem(repo="o/r", number=4, kind="issue", state="open", title="t", body="b")
    set_runner_hold(unlisted, reason="bounced at opus; fable needs approval", now=NOW)
    db.add_all([runner, unlisted])
    person = _held(db, 2)
    evidence = _held(db, 3, kind="evidence")
    db.flush()

    reconciler = WorkItem(repo="o/r", number=5, kind="issue", state="open", title="t", body="b",
                          hold_kind="owner_action", hold_reason="r", hold_by="reconciler")
    agent = WorkItem(repo="o/r", number=6, kind="issue", state="open", title="t", body="b",
                     hold_kind="owner_action", hold_reason="r", hold_by="session:s1")
    db.add_all([reconciler, agent])
    db.flush()

    released = release_policy_skipped_holds(
        db, ["o/r#1", "o/r#2", "o/r#3", "o/r#5", "o/r#6", "o/r#9", "garbage"])
    db.commit()
    assert sorted(released) == ["o/r#1", "o/r#5"]
    assert reconciler.hold_kind is None and agent.hold_kind == "owner_action"
    assert runner.hold_kind is None and runner.hold_fingerprint is None
    assert person.hold_kind == "owner_action"
    assert evidence.hold_kind == "evidence"
    assert unlisted.hold_kind == "owner_action"
    assert "policy_skip" in RELEASE_SIGNALS
    rows = db.query(AuditEvent).filter(AuditEvent.type == "pm.hold.release").all()
    assert [(r.detail["signal"], r.detail["kind"], r.detail["actorClass"], r.via)
            for r in rows] == [("policy_skip", "owner_action", "system", "runner")] * 2
    assert release_policy_skipped_holds(db, ["o/r#1"]) == []
    assert release_policy_skipped_holds(db, []) == []


def test_release_policy_skipped_holds_records_the_caller_as_actor():
    """The needs-you hygiene loop releases as its own automation actor, and a person
    applying a hygiene recommendation as themselves — never a person's own hold."""
    from server.db.models import AuditEvent
    from server.pm.holds import release_policy_skipped_holds, set_runner_hold
    db = _sync_db()
    runner = WorkItem(repo="o/r", number=1, kind="issue", state="open", title="t", body="b")
    set_runner_hold(runner, reason="bounced at opus; fable needs approval", now=NOW)
    db.add(runner)
    person = _held(db, 2)
    db.flush()
    released = release_policy_skipped_holds(db, ["o/r#1", "o/r#2"], actor="tony@example.com",
                                             via="api")
    assert released == ["o/r#1"] and person.hold_kind == "owner_action"
    row = db.query(AuditEvent).filter(AuditEvent.type == "pm.hold.release").one()
    assert (row.identity, row.via, row.detail["actorClass"]) == ("tony@example.com", "api",
                                                                 "person")


def test_release_by_signals_ignores_mirror_lag_from_an_edit_made_before_the_hold():
    """#2985: the hold fingerprints the lagging mirror body; the triage's own pre-hold
    edit then lands in the mirror and drifts it. GitHub's updatedAt predates the hold,
    so that drift is not a release — only an edit after the hold is."""
    from datetime import timedelta
    from server.pm.holds import release_by_signals
    db = _sync_db()
    lagged = _held(db, 1)
    edited_after = _held(db, 2)
    for wi in (lagged, edited_after):
        wi.hold_set_at = NOW
        wi.body = "edited"
    lagged.gh_updated_at = NOW - timedelta(minutes=1)
    edited_after.gh_updated_at = NOW + timedelta(minutes=1)
    db.flush()
    released = release_by_signals(db, human_edits=[], now=NOW)
    assert [(w.number, s) for w, s in released] == [(2, "body_edit")]
    assert lagged.hold_kind == "owner_action"


# --- The owner rule (assistant triage tools, gap log 2026-10-11) ------------------------
# ONE function decides whether an actor may release a hold — or the ``parked`` label — that
# someone else placed. ``release_via_api`` (Retry, Implement now / dispatch_backlog, the
# bulk release endpoint) all route through it, so "same rule as dispatch_backlog" is true.

OP = "tony@example.com"
ASSIST = f"assistant:{OP}"


@pytest.mark.parametrize("kind,placed_by,actor,operator,refused", [
    # A person (any person) keeps today's behaviour: they may release any non-decision hold.
    ("owner_action", "someone@example.com", OP, OP, None),
    ("evidence", "runner", OP, OP, None),
    # The assistant, on its operator's confirmed card: system- and agent-placed holds go…
    ("owner_action", "runner", ASSIST, OP, None),
    ("owner_action", "automation:error-triage", ASSIST, OP, None),
    ("owner_action", "session:abc", ASSIST, OP, None),
    ("evidence", "automation:error-triage", ASSIST, OP, None),
    ("stronger_model", "runner", ASSIST, OP, None),
    # …so does a hold the operator placed themselves…
    ("owner_action", OP, ASSIST, OP, None),
    # …but another person's hold, or one whose placer is unknown, is refused.
    ("owner_action", "someone@example.com", ASSIST, OP, "someone@example.com"),
    ("evidence", "someone@example.com", ASSIST, OP, "someone@example.com"),
    ("owner_action", None, ASSIST, OP, "not recorded"),
    # Other agents keep the class gate: only the agent-settable kinds.
    ("owner_action", "runner", "lan@local", "lan@local", "may not clear"),
    ("evidence", "someone@example.com", "session:x", "session:x", None),
    # The parked label: the placer is a GitHub login, the operator their gh login.
    ("parked", "steiner385", ASSIST, "steiner385", None),
    ("parked", "renovate[bot]", ASSIST, "steiner385", None),
    ("parked", "someone-else", ASSIST, "steiner385", "someone-else"),
    ("parked", None, ASSIST, "steiner385", "not recorded"),
    ("parked", "steiner385", ASSIST, None, "GitHub login"),
    ("parked", "someone-else", OP, None, None),
    ("parked", "steiner385", "lan@local", "lan@local", "may not clear"),
])
def test_release_refusal_is_the_owner_rule(kind, placed_by, actor, operator, refused):
    from server.pm.holds import release_refusal
    out = release_refusal(kind, placed_by, actor=actor, operator=operator)
    if refused is None:
        assert out is None
    else:
        assert out is not None and refused in out


def test_release_refusal_refuses_a_decision_hold_for_everyone():
    from server.pm.holds import DECISION_HOLD_MESSAGE, release_refusal
    for actor in (OP, ASSIST, "runner"):
        assert release_refusal("decision", OP, actor=actor, operator=OP) == DECISION_HOLD_MESSAGE
    assert release_refusal(None, None, actor=ASSIST, operator=OP) is None


def test_parked_kind_is_the_queues_parked_label_and_skip_reason():
    from server.pm.holds import PARKED
    from server.pm.queue import PARKED_LABELS, SKIP_REASONS
    assert PARKED in PARKED_LABELS and PARKED in SKIP_REASONS


def test_release_via_api_applies_the_owner_rule_and_records_the_reason(tmp_path):
    """The assistant may release a runner's hold on its operator's card (audited with
    the operator's reason) but not another person's — the same function decides both."""
    import asyncio
    from sqlalchemy import select
    from server.db import db_session, init_db
    from server.db.models import AuditEvent
    from server.pm.holds import release_via_api

    async def _run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
        async with db_session() as db:
            runner = WorkItem(id="w1", repo="o/r", number=1, kind="issue", title="t", body="b")
            _park(runner, "owner_action", actor="runner")
            person = WorkItem(id="w2", repo="o/r", number=2, kind="issue", title="t", body="b")
            _park(person, "owner_action", actor="someone@example.com")
            db.add_all([runner, person])
            await db.flush()
            assert await release_via_api(db, runner, actor=ASSIST, identity=OP,
                                         signal="retry", reason="stale bounce",
                                         via="assistant") == "owner_action"
            with pytest.raises(HoldPermissionError, match="someone@example.com"):
                await release_via_api(db, person, actor=ASSIST, identity=OP, signal="retry")
            assert person.hold_kind == "owner_action"
            rows = (await db.execute(select(AuditEvent).where(
                AuditEvent.type == "pm.hold.release"))).scalars().all()
            assert [(r.detail["workItemId"], r.detail["reason"], r.detail["actorClass"], r.via)
                    for r in rows] == [("w1", "stale bounce", "agent", "assistant")]
    asyncio.run(_run())
