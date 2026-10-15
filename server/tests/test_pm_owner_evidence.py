"""The ``owner_evidence`` hold kind (#3113): proof only the owner can supply — a check
on a real device, a legal record — with a request, a precondition and reminders."""
from datetime import datetime, timedelta, timezone

import pytest

from server.db.models import WorkItem
from server.pm.desired_state import HELD_KINDS
from server.pm.holds import (
    AGENT_SETTABLE, OWNER_EVIDENCE, RUNNER_ACTOR, EvidenceRequest, HoldPermissionError,
    clear_hold, evidence_reminder_due, evidence_surfaced, set_hold, set_owner_evidence_hold,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _wi(number=1):
    return WorkItem(repo="o/r", number=number, kind="issue", state="open", title="t", body="b")


def _request(**kw):
    return EvidenceRequest(**{"request": "Verify the portrait wall on the kitchen tablet",
                              "where": "a comment on this issue", **kw})


def test_owner_evidence_parks_the_card_and_is_never_agent_settable():
    assert OWNER_EVIDENCE == "owner_evidence"
    assert OWNER_EVIDENCE in HELD_KINDS
    assert OWNER_EVIDENCE not in AGENT_SETTABLE
    with pytest.raises(HoldPermissionError):
        set_owner_evidence_hold(_wi(), request=_request(), reason="r", actor="session:s1", now=NOW)


def test_request_round_trips_and_reads_a_damaged_row_tolerantly():
    req = _request(not_before=NOW, after_pr="o/r#7", remind_every_days=5,
                   last_reminded_at=NOW - timedelta(days=1))
    assert EvidenceRequest.from_wire(req.to_wire()) == req
    # A row from a hand edit or an older writer still reads; the hold reason stands in.
    damaged = EvidenceRequest.from_wire({"notBefore": "nonsense", "remindEveryDays": -2},
                                        fallback_request="needs owner record")
    assert damaged.request == "needs owner record"
    assert damaged.not_before is None
    assert damaged.remind_every_days == EvidenceRequest.DEFAULT_REMIND_EVERY_DAYS
    assert EvidenceRequest.from_wire(None, fallback_request="x").request == "x"


def test_set_owner_evidence_hold_records_the_request_and_any_other_hold_clears_it():
    wi = _wi()
    set_owner_evidence_hold(wi, request=_request(), reason="needs physical verification",
                            actor=RUNNER_ACTOR, now=NOW)
    assert (wi.hold_kind, wi.hold_by, wi.hold_set_at) == (OWNER_EVIDENCE, RUNNER_ACTOR, NOW)
    assert wi.hold_reason == "needs physical verification"
    assert wi.hold_detail["request"].startswith("Verify the portrait wall")
    assert wi.hold_fingerprint
    # A different kind written over it drops the request with it — a stale request
    # must never ride along on an owner_action hand-off.
    set_hold(wi, kind="owner_action", reason="r", actor="tony@example.com", now=NOW)
    assert wi.hold_detail is None
    set_owner_evidence_hold(wi, request=_request(), reason="r", actor="tony@example.com", now=NOW)
    clear_hold(wi, actor="tony@example.com")
    assert wi.hold_kind is None and wi.hold_detail is None


def test_set_hold_refuses_owner_evidence_without_a_request():
    """``set_owner_evidence_hold`` is the ONE writer: a bare ``set_hold`` would park the
    item with nothing to ask for."""
    with pytest.raises(ValueError):
        set_hold(_wi(), kind=OWNER_EVIDENCE, reason="r", actor="tony@example.com", now=NOW)


@pytest.mark.parametrize("kw,merged,surfaced", [
    ({}, set(), True),
    ({"not_before": NOW + timedelta(hours=1)}, set(), False),
    ({"not_before": NOW - timedelta(hours=1)}, set(), True),
    ({"after_pr": "o/r#7"}, set(), False),
    ({"after_pr": "o/r#7"}, {"o/r#7"}, True),
])
def test_a_request_surfaces_only_once_its_precondition_is_met(kw, merged, surfaced):
    assert evidence_surfaced(_request(**kw), now=NOW, merged_refs=merged) is surfaced


def test_reminders_follow_the_schedule_from_the_hold_or_the_last_reminder():
    req = _request(remind_every_days=3)
    set_at = NOW - timedelta(days=2)
    assert not evidence_reminder_due(req, hold_set_at=set_at, now=NOW)
    assert evidence_reminder_due(req, hold_set_at=NOW - timedelta(days=3), now=NOW)
    reminded = _request(remind_every_days=3, last_reminded_at=NOW - timedelta(days=1))
    assert not evidence_reminder_due(reminded, hold_set_at=NOW - timedelta(days=30), now=NOW)
    # No schedule (0) never reminds; a naive DB timestamp is UTC.
    assert not evidence_reminder_due(_request(remind_every_days=0),
                                     hold_set_at=NOW - timedelta(days=30), now=NOW)
    assert evidence_reminder_due(req, hold_set_at=(NOW - timedelta(days=4)).replace(tzinfo=None),
                                 now=NOW)


def _sync_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from server.db.models import Base
    e = create_engine("sqlite://")
    Base.metadata.create_all(e)
    return Session(e)


def test_a_board_move_releases_it_but_a_body_edit_does_not():
    """The owner may paste the evidence into the issue body: that is what the evidence
    check reads, not a hand-back. Moving the card to Ready/Backlog still is one."""
    from server.pm.holds import release_by_signals
    from server.pm.mirror_store import HumanEdit
    db = _sync_db()
    moved, edited = _wi(1), _wi(2)
    for wi in (moved, edited):
        set_owner_evidence_hold(wi, request=_request(), reason="r", actor=RUNNER_ACTOR, now=NOW)
        db.add(wi)
    db.flush()
    edited.body = "here is the photo: …"
    db.flush()
    released = release_by_signals(db, human_edits=[HumanEdit("o/r#1", "Status", "Held", "Ready")],
                                  now=NOW)
    assert [(w.number, s) for w, s in released] == [(1, "board_edit")]
    assert moved.hold_kind is None and moved.hold_detail is None
    assert edited.hold_kind == OWNER_EVIDENCE


def test_release_on_evidence_supplied_ends_only_the_owner_evidence_hold_it_judged():
    from server.db.models import AuditEvent
    from server.pm.holds import release_on_evidence_supplied
    db = _sync_db()
    wi = _wi(1)
    set_owner_evidence_hold(wi, request=_request(), reason="r", actor=RUNNER_ACTOR, now=NOW)
    other = _wi(2)
    set_hold(other, kind="owner_action", reason="r", actor=RUNNER_ACTOR, now=NOW)
    db.add_all([wi, other])
    db.flush()
    assert not release_on_evidence_supplied(db, "o/r#2", actor="automation:needs-you", via="x")
    assert not release_on_evidence_supplied(db, "o/r#1", actor="automation:needs-you", via="x",
                                            hold_set_at=NOW - timedelta(days=1))
    assert release_on_evidence_supplied(db, "o/r#1", actor="automation:needs-you", via="x",
                                        hold_set_at=NOW)
    assert wi.hold_kind is None and other.hold_kind == "owner_action"
    row = db.query(AuditEvent).filter(AuditEvent.type == "pm.hold.release").one()
    assert row.detail["signal"] == "evidence_supplied" and row.detail["kind"] == OWNER_EVIDENCE


def test_stamp_evidence_reminded_records_the_reminder_on_the_same_ask_only():
    from server.pm.holds import stamp_evidence_reminded
    db = _sync_db()
    wi = _wi(1)
    set_owner_evidence_hold(wi, request=_request(), reason="r", actor=RUNNER_ACTOR, now=NOW)
    db.add(wi)
    db.flush()
    later = NOW + timedelta(days=3)
    assert not stamp_evidence_reminded(db, "o/r#1", hold_set_at=NOW - timedelta(days=1), now=later)
    assert stamp_evidence_reminded(db, "o/r#1", hold_set_at=NOW, now=later)
    db.commit()
    req = EvidenceRequest.from_wire(db.get(WorkItem, wi.id).hold_detail)
    assert req.last_reminded_at == later and req.request.startswith("Verify the portrait")
    assert not evidence_reminder_due(req, hold_set_at=NOW, now=later + timedelta(days=2))


def test_the_digest_lists_an_evidence_ask_in_its_own_group():
    """#3113 acceptance: evidence-only items are grouped apart from what needs deciding;
    a reply turns into the hygiene's close recommendation, which stands in for it."""
    from server.pm import digest as dg
    from server.pm.escalation import ATTENTION_EVIDENCE, HeldItem
    held = HeldItem(ref="o/r#1", repo="o/r", number=1, product="p", title="t", url="u",
                    hold_kind=OWNER_EVIDENCE, hold_reason="needs owner record", hold_by="runner",
                    hold_set_at=NOW, age_hours=1.0, proposal_id=None, options=(),
                    recommended_key=None, downstream=0, downstream_dispatchable=0,
                    downstream_refs=(), dispatchable_on_release=False, released_by="",
                    work_item_id="wi1", attention_kind=ATTENTION_EVIDENCE,
                    evidence={"request": "Record the DPA acceptance dates"})
    ask = dg.from_held(held)
    assert ask.kind == dg.KIND_EVIDENCE and ask.recommendation is None
    assert ask.why == "Record the DPA acceptance dates. Post it as a comment on this issue."
    gate = dg.from_initiative("p", {"slug": "s", "title": "S", "status": "awaiting_human"})
    out = dg.assemble([ask, gate], products=["p"], now=NOW)
    [product] = out["products"]
    assert [i["id"] for i in product["items"]] == ["gate:s"]
    assert [i["id"] for i in product["ownerEvidence"]] == ["hold:wi1"]
    assert out["total"] == 2
    # Once the owner replied, the close recommendation stands in for the ask.
    rec = dg.from_recommendation({"id": "r1", "product": "p", "ref": "o/r#1",
                                  "rule": "evidence_posted", "action": "close",
                                  "detail": {"title": "t"}, "evidence": "replied"})
    assert rec.recommendation.label == "Close — evidence posted"
    out = dg.assemble([ask, rec], products=["p"], now=NOW)
    assert [i["id"] for i in out["products"][0]["items"]] == ["rec:r1"]
    assert out["products"][0]["ownerEvidence"] == []
