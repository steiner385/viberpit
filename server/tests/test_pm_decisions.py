"""Decision proposals (spec 2026-09-18 §6): the table, the one service, the holds.

Every timestamp here is derived from ``datetime.now`` rather than written as a
literal date, so nothing in this file can start failing on a particular calendar
day.
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.db.models import AuditEvent, Base, DecisionProposal, WorkItem
from server.pm import decisions
from server.pm.decisions import DecisionError

NOW = datetime.now(timezone.utc).replace(microsecond=0)
LATER = NOW + timedelta(minutes=5)

OPTIONS = [
    {"key": "per_repo", "label": "Per repo", "detail": "one toggle on each repo row"},
    {"key": "global", "label": "Global", "detail": None},
]


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/decisions.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _item(db, number=1, **kw):
    wi = WorkItem(repo="o/r", number=number, kind="issue", state="open",
                  title="t", body="b", **kw)
    db.add(wi)
    await db.flush()
    return wi


async def _audits(db, type_):
    return (await db.execute(select(AuditEvent).where(
        AuditEvent.type == type_))).scalars().all()


@pytest.mark.asyncio
async def test_propose_writes_the_row_the_hold_and_both_audits(db):
    wi = await _item(db)
    proposal = await decisions.propose(
        db, wi, options=OPTIONS, recommended_key="per_repo",
        rationale="Per repo matches how people already think about enabling this.",
        actor="session:s1", now=NOW, via="session")
    await db.commit()

    assert proposal.status == "pending"
    assert proposal.work_item_id == wi.id
    assert proposal.proposed_by == "session:s1"
    # session_id is DERIVED from the actor, never passed separately, so the two
    # can never disagree about who proposed.
    assert proposal.session_id == "s1"
    assert proposal.recommended_key == "per_repo"
    assert proposal.chosen_key is None and proposal.resolved_at is None
    assert [o["key"] for o in proposal.options] == ["per_repo", "global"]

    # the hold is written in the same unit of work, by the same actor
    assert (wi.hold_kind, wi.hold_by) == ("decision", "session:s1")
    assert wi.hold_reason.startswith(decisions.HOLD_REASON_PREFIX)
    assert wi.hold_set_at == NOW and wi.hold_fingerprint

    proposed = await _audits(db, "pm.decision.propose")
    assert len(proposed) == 1
    assert proposed[0].detail == {"workItemId": wi.id, "proposalId": proposal.id,
                                  "actorClass": "agent"}
    assert proposed[0].via == "session"
    hold_set = await _audits(db, "pm.hold.set")
    assert len(hold_set) == 1
    assert hold_set[0].detail["kind"] == "decision"
    assert hold_set[0].detail["reason"] == wi.hold_reason


@pytest.mark.asyncio
async def test_hold_reason_quotes_a_bounded_lead_in_of_the_rationale(db):
    wi = await _item(db)
    long_rationale = "x" * 400
    await decisions.propose(db, wi, options=OPTIONS, recommended_key="global",
                            rationale=long_rationale, actor="tony@example.com", now=NOW)
    await db.flush()
    assert wi.hold_reason == (decisions.HOLD_REASON_PREFIX
                              + "x" * decisions.HOLD_REASON_RATIONALE_CHARS)


@pytest.mark.asyncio
async def test_a_second_pending_proposal_on_the_same_item_is_refused(db):
    wi = await _item(db)
    await decisions.propose(db, wi, options=OPTIONS, recommended_key="global",
                            rationale="first", actor="session:s1", now=NOW)
    await db.flush()
    with pytest.raises(DecisionError, match="already pending"):
        await decisions.propose(db, wi, options=OPTIONS, recommended_key="global",
                                rationale="second", actor="session:s2", now=LATER)
    assert len((await db.execute(select(DecisionProposal))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_one_pending_proposal_per_item_is_enforced_by_the_database_too(db):
    """The SELECT in ``propose`` produces the friendly message, but two concurrent
    proposers can both pass it inside their own transaction — so the partial unique
    index is the actual arbiter. Resolved rows stay outside it."""
    from sqlalchemy.exc import IntegrityError
    wi = await _item(db)
    await decisions.propose(db, wi, options=OPTIONS, recommended_key="global",
                            rationale="first", actor="session:s1", now=NOW)
    await db.commit()
    db.add(DecisionProposal(work_item_id=wi.id, options=OPTIONS, recommended_key="global",
                            rationale="racing insert", status="pending",
                            proposed_by="session:s2", created_at=LATER))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


@pytest.mark.asyncio
async def test_a_persons_park_wins_but_a_machines_does_not(db):
    """Spec §6: a session that finds a person's park reports back rather than
    overwriting it. The runner's own hand-off is superseded — the decision is the
    more specific answer to "why is this stopped"."""
    from server.pm.holds import RUNNER_ACTOR, set_runner_hold
    parked = await _item(db, 1)
    parked.hold_kind, parked.hold_by = "owner_action", "tony@example.com"
    parked.hold_reason, parked.hold_set_at = "I will look at this", NOW
    await db.flush()
    with pytest.raises(DecisionError, match="parked"):
        await decisions.propose(db, parked, options=OPTIONS, recommended_key="global",
                                rationale="r", actor="session:s1", now=NOW)
    assert parked.hold_kind == "owner_action" and parked.hold_by == "tony@example.com"
    assert (await db.execute(select(DecisionProposal))).scalars().all() == []

    handed_back = await _item(db, 2)
    set_runner_hold(handed_back, reason="bounced at the top rung", now=NOW)
    await db.flush()
    await decisions.propose(db, handed_back, options=OPTIONS, recommended_key="global",
                            rationale="r", actor="session:s1", now=NOW)
    assert (handed_back.hold_kind, handed_back.hold_by) == ("decision", "session:s1")
    assert RUNNER_ACTOR != handed_back.hold_by


@pytest.mark.asyncio
@pytest.mark.parametrize("options,recommended,rationale,match", [
    ([OPTIONS[0]], "per_repo", "r", "between 2 and 6"),
    ([{"key": f"k{i}", "label": f"l{i}"} for i in range(7)], "k0", "r", "between 2 and 6"),
    ([{"key": "a", "label": "A"}, {"key": "a", "label": "Again"}], "a", "r", "duplicate"),
    ([{"key": " ", "label": "A"}, {"key": "b", "label": "B"}], "b", "r", "non-empty key"),
    ([{"key": "a", "label": ""}, {"key": "b", "label": "B"}], "b", "r", "non-empty label"),
    (["not-an-object", {"key": "b", "label": "B"}], "b", "r", "object"),
    (OPTIONS, "nope", "r", "not one of"),
    (OPTIONS, "global", "   ", "rationale is required"),
])
async def test_propose_validates_its_input(db, options, recommended, rationale, match):
    wi = await _item(db)
    with pytest.raises(DecisionError, match=match):
        await decisions.propose(db, wi, options=options, recommended_key=recommended,
                                rationale=rationale, actor="session:s1", now=NOW)
    assert wi.hold_kind is None
    assert (await db.execute(select(DecisionProposal))).scalars().all() == []


@pytest.mark.asyncio
async def test_accept_records_the_key_and_releases_the_hold(db):
    wi = await _item(db)
    proposal = await decisions.propose(
        db, wi, options=OPTIONS, recommended_key="per_repo", rationale="because",
        actor="session:s1", now=NOW)
    await db.flush()

    with pytest.raises(DecisionError, match="not one of"):
        await decisions.accept(db, proposal, chosen_key="neither",
                               actor="tony@example.com", now=LATER)
    assert wi.hold_kind == "decision"

    await decisions.accept(db, proposal, chosen_key="global",
                           actor="tony@example.com", now=LATER)
    await db.commit()
    assert proposal.status == "accepted" and proposal.chosen_key == "global"
    assert proposal.resolved_by == "tony@example.com" and proposal.resolved_at == LATER
    assert wi.hold_kind is None and wi.hold_by is None and wi.hold_set_at is None

    accepted = await _audits(db, "pm.decision.accept")
    assert len(accepted) == 1
    assert accepted[0].detail["proposalId"] == proposal.id
    assert accepted[0].detail["chosenKey"] == "global"
    released = await _audits(db, "pm.hold.release")
    assert len(released) == 1
    assert released[0].detail["kind"] == "decision"
    assert released[0].detail["signal"] == "decision"   # holds.RELEASE_SIGNALS
    assert released[0].detail["actorClass"] == "person"

    # resolved is resolved: a second accept cannot re-clear a hold set since.
    with pytest.raises(DecisionError, match="already accepted"):
        await decisions.accept(db, proposal, chosen_key="per_repo",
                               actor="tony@example.com", now=LATER)


@pytest.mark.asyncio
async def test_reject_parks_the_item_as_the_persons_owner_action(db):
    from server.pm.holds import DECISION_REJECT_REASON
    wi = await _item(db)
    proposal = await decisions.propose(
        db, wi, options=OPTIONS, recommended_key="per_repo", rationale="because",
        actor="session:s1", now=NOW)
    await db.flush()
    await decisions.reject(db, proposal, actor="tony@example.com", now=LATER)
    await db.commit()

    assert proposal.status == "rejected" and proposal.chosen_key is None
    assert proposal.resolved_by == "tony@example.com" and proposal.resolved_at == LATER
    assert wi.hold_kind == "owner_action"
    assert wi.hold_reason == DECISION_REJECT_REASON
    assert wi.hold_by == "tony@example.com"     # the person, recorded truthfully
    assert wi.hold_set_at == LATER

    assert len(await _audits(db, "pm.decision.reject")) == 1
    hold_rows = await _audits(db, "pm.hold.set")
    assert [r.detail["kind"] for r in hold_rows] == ["decision", "owner_action"]
    assert hold_rows[-1].detail["reason"] == DECISION_REJECT_REASON
    # a rejection releases nothing — the item stays parked, just by a person now
    assert await _audits(db, "pm.hold.release") == []
    with pytest.raises(DecisionError, match="already rejected"):
        await decisions.reject(db, proposal, actor="tony@example.com", now=LATER)


@pytest.mark.asyncio
async def test_an_accepted_item_can_take_a_new_decision_later(db):
    """Only a PENDING proposal blocks a new one — an issue can be decided more
    than once over its life."""
    wi = await _item(db)
    first = await decisions.propose(db, wi, options=OPTIONS, recommended_key="global",
                                    rationale="first", actor="session:s1", now=NOW)
    await decisions.accept(db, first, chosen_key="global", actor="tony@example.com", now=NOW)
    second = await decisions.propose(db, wi, options=OPTIONS, recommended_key="per_repo",
                                     rationale="second", actor="session:s2", now=LATER)
    await db.commit()
    assert second.status == "pending" and wi.hold_kind == "decision"


@pytest.mark.asyncio
async def test_accepted_text_for_refs_is_the_one_verbatim_formatter(db):
    wi = await _item(db, 1)
    other = await _item(db, 2)
    undecided = await _item(db, 3)
    p1 = await decisions.propose(db, wi, options=OPTIONS, recommended_key="per_repo",
                                 rationale="because it matches the mental model",
                                 actor="session:s1", now=NOW)
    await decisions.accept(db, p1, chosen_key="per_repo", actor="tony@example.com", now=NOW)
    # an option with no detail falls back to its label on BOTH sides of the colon
    p2 = await decisions.propose(db, other, options=OPTIONS, recommended_key="global",
                                 rationale="one switch is enough",
                                 actor="session:s2", now=NOW)
    await decisions.accept(db, p2, chosen_key="global", actor="tony@example.com", now=NOW)
    # a pending proposal contributes nothing
    await decisions.propose(db, undecided, options=OPTIONS, recommended_key="global",
                            rationale="still open", actor="session:s3", now=NOW)
    await db.commit()

    got = await decisions.accepted_text_for_refs(
        db, ["o/r#1", "o/r#2", "o/r#3", "o/r#404", "not-a-ref"])
    assert got == {
        "o/r#1": ("Decision (Per repo): one toggle on each repo row — "
                  "because it matches the mental model"),
        "o/r#2": "Decision (Global): Global — one switch is enough",
    }
    assert await decisions.accepted_text_for_refs(db, []) == {}


@pytest.mark.asyncio
async def test_accepted_text_for_refs_carries_the_decision_in_force(db):
    """Two accepted decisions on one issue: the prompt gets the newest."""
    wi = await _item(db)
    first = await decisions.propose(db, wi, options=OPTIONS, recommended_key="global",
                                    rationale="first", actor="session:s1", now=NOW)
    await decisions.accept(db, first, chosen_key="global", actor="tony@example.com", now=NOW)
    second = await decisions.propose(db, wi, options=OPTIONS, recommended_key="per_repo",
                                     rationale="second", actor="session:s2", now=LATER)
    await decisions.accept(db, second, chosen_key="per_repo",
                           actor="tony@example.com", now=LATER)
    await db.commit()
    assert await decisions.accepted_text_for_refs(db, ["o/r#1"]) == {
        "o/r#1": "Decision (Per repo): one toggle on each repo row — second"}


@pytest.mark.asyncio
async def test_pending_list_pairs_each_proposal_with_its_work_item(db):
    wi = await _item(db, 1)
    other = await _item(db, 2)
    pending = await decisions.propose(db, wi, options=OPTIONS, recommended_key="global",
                                      rationale="open", actor="session:s1", now=NOW)
    resolved = await decisions.propose(db, other, options=OPTIONS, recommended_key="global",
                                       rationale="closed", actor="session:s2", now=NOW)
    await decisions.accept(db, resolved, chosen_key="global",
                           actor="tony@example.com", now=LATER)
    # an orphaned proposal (its item un-mirrored) still reaches the inbox
    orphan = DecisionProposal(work_item_id="gone", options=OPTIONS, recommended_key="global",
                              rationale="orphan", status="pending", proposed_by="session:s3",
                              created_at=LATER)
    db.add(orphan)
    await db.commit()

    rows = await decisions.pending_list(db)
    assert [(p.id, w.id if w else None) for p, w in rows] == [
        (orphan.id, None), (pending.id, wi.id)]   # newest first
