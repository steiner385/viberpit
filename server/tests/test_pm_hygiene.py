"""Pure needs-you hygiene rules (design 2026-10-14 §2, ``server/pm/hygiene.py``)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from server.pm import hygiene as h
from server.pm.queue import QueueRow
from server.pm.stale_deps import DEP_PRUNE_MAX, StaleEdge

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(hours=25)


def _row(number: int = 1, **over: Any) -> QueueRow:
    base: dict[str, Any] = dict(
        ref=f"o/r#{number}", repo="o/r", number=number, product="p", title=f"Issue {number}",
        body="", url="u", status="Ready", score=None, gh_created_at=NOW, gh_updated_at=NOW,
        assignees=(), author="tony", labels=(), open_children=0, open_blockers=(),
        live_session=False, claimed=False, work_item_id=f"wi{number}")
    base.update(over)
    return QueueRow(**base)


def _inputs(rows=(), *, policy=None, board=None, edges=()) -> h.ProductInputs:
    return h.ProductInputs(slug="p", project_id="pid", policy=policy or {}, rows=list(rows),
                           board_updated_at=board or {}, stale_edges=list(edges))


# --- rule 1: done_but_open -------------------------------------------------------------

def test_done_but_open_closes_a_card_done_for_over_a_day():
    rows = [_row(1, status="Done"), _row(2, status="Done"), _row(3, status="Ready")]
    board = {"wi1": OLD, "wi2": NOW - timedelta(hours=23), "wi3": OLD}
    out = h.done_but_open(rows, board, {}, now=NOW)
    assert [(a.rule, a.ref, a.action, a.work_item_id) for a in out] == [
        ("done_but_open", "o/r#1", "close", "wi1")]
    assert "Done" in out[0].evidence


@pytest.mark.parametrize("over", [
    # Renovate's Dependency Dashboard (bot noise by title).
    dict(title="Dependency Dashboard"),
    # A self-closing tracker / daily auto-upserted report (cairnea/KinDash#12032, #16677).
    dict(title="Nightly CI Scoreboard", labels=("nightly-scoreboard",),
         author="app/github-actions"),
    dict(title="traceability-drift — 2026-10-14", labels=("traceability-drift",),
         author="app/github-actions"),
    dict(title="📊 CI / Delivery Observability Digest"),
    # A product's own skip label.
    dict(labels=("ops-noise",)),
    # Somebody is on it, or it is still an open epic, or it is held.
    dict(live_session=True), dict(claimed=True), dict(open_children=2),
    dict(hold_kind="owner_action", hold_by="runner"),
    # No board timestamp: never guessed.
    dict(work_item_id="missing"),
])
def test_done_but_open_skips_noise_trackers_and_live_work(over):
    rows = [_row(1, status="Done", **over)]
    assert h.done_but_open(rows, {"wi1": OLD}, {"p": {"skip_labels": ["ops-noise"]}},
                           now=NOW) == []


# --- rule 2: board_blocked_no_blockers --------------------------------------------------

def test_board_blocked_with_no_open_blocker_returns_to_the_queue_floor():
    rows = [_row(1, status="Blocked"), _row(2, status="Blocked", open_blockers=("o/r#9",)),
            _row(3, status="Blocked", hold_kind="decision", hold_by="tony"),
            _row(4, status="Blocked", labels=("red-watchdog",)),
            _row(5, status="Blocked", live_session=True), _row(6, status="Ready")]
    out = h.board_blocked_no_blockers(rows, {"p": {"queue_min_status": "Backlog"}})
    assert [(a.ref, a.action, a.detail["status"]) for a in out] == [
        ("o/r#1", "set_status", "Backlog")]
    # No policy row: the schema default (Ready).
    assert [a.detail["status"] for a in h.board_blocked_no_blockers(rows[:1], {})] == ["Ready"]


# --- rule 3: stale_holds (the shared policy_skip rule) ----------------------------------

def test_stale_holds_releases_system_holds_the_queue_already_skips():
    rows = [_row(1, hold_kind="owner_action", hold_by="runner", labels=("dependencies",),
                 hold_reason="bounced"),
            _row(2, hold_kind="owner_action", hold_by="tony", labels=("dependencies",),
                 hold_reason="mine")]
    out = h.stale_holds(rows, {}, now=NOW)
    assert [(a.rule, a.ref, a.action, a.detail["holdBy"]) for a in out] == [
        ("stale_holds", "o/r#1", "release_hold", "runner")]


# --- rule 7: evidence_posted (#3113, ported from #3128) --------------------------------

def _ask(number, **over):
    base = dict(hold_kind="owner_evidence", hold_by="runner",
                hold_set_at=NOW - timedelta(hours=5),
                hold_reason="needs physical verification (declared by the session)",
                hold_detail={"request": "Verify the kitchen tablet shows the new layout"},
                last_human_comment_at=NOW)
    base.update(over)
    return _row(number, **base)


def test_evidence_posted_closes_an_owner_evidence_ask_a_human_answered():
    """A human comment AFTER an ``owner_evidence`` hold → close as completed, carrying
    the ask's ``holdSetAt`` so the write re-checks it; a comment before the ask, a
    missing timestamp, another hold kind, an item somebody is on, or an ask still
    waiting on its precondition → nothing. Recommend-only."""
    asked = NOW - timedelta(hours=5)
    rows = [_ask(1),
            _ask(2, last_human_comment_at=asked - timedelta(minutes=1)),
            _ask(3, last_human_comment_at=None),
            _ask(4, hold_set_at=None),
            _ask(5, hold_kind="owner_action"),
            _ask(6, live_session=True),
            _ask(7, evidence_waiting=True)]
    out = h.evidence_posted(rows)
    assert [(a.rule, a.ref, a.action, a.work_item_id) for a in out] == [
        ("evidence_posted", "o/r#1", "close", "wi1")]
    [a] = out
    assert a.detail["closeReason"] == "completed"
    assert a.detail["holdSetAt"] == asked.isoformat()
    assert a.detail["comment"].startswith("The evidence asked for on 2026-10-14 was posted here")
    assert "Asked: Verify the kitchen tablet" in a.detail["comment"]
    assert "2026-10-14 12:00 UTC" in a.evidence
    assert h.RULE_EVIDENCE_POSTED in h.RECOMMEND_ONLY_RULES and h.RULE_EVIDENCE_POSTED in h.RULES
    assert [x.ref for x in h.plan(_inputs(rows), now=NOW) if x.rule == "evidence_posted"] == ["o/r#1"]
    # A naive timestamp (SQLite hands them back tz-less) compares like an aware one.
    assert h.evidence_replied(asked.replace(tzinfo=None), NOW)
    assert not h.evidence_replied(NOW, asked.replace(tzinfo=None))


def test_evidence_reminders_are_due_only_for_an_open_unanswered_surfaced_ask():
    old = NOW - timedelta(days=4)
    rows = [_ask(1, hold_set_at=old, last_human_comment_at=None),          # due
            _ask(2, hold_set_at=NOW - timedelta(days=1), last_human_comment_at=None),
            _ask(3, hold_set_at=old),                                       # answered
            _ask(4, hold_set_at=old, last_human_comment_at=None, evidence_waiting=True),
            _ask(5, hold_set_at=old, last_human_comment_at=None,
                 hold_detail={"request": "x", "remindEveryDays": 0}),       # no schedule
            _ask(6, hold_set_at=old, last_human_comment_at=None,
                 hold_detail={"request": "x", "lastRemindedAt": NOW.isoformat()}),
            _ask(7, hold_kind="owner_action", hold_set_at=old, last_human_comment_at=None)]
    assert [r.ref for r in h.evidence_reminders_due(rows, now=NOW)] == ["o/r#1"]


# --- rule 4: resolved_edges -------------------------------------------------------------

def test_resolved_edges_lists_only_prunable_edges_one_target_per_edge():
    edges = [StaleEdge("o/r#1", "o/r#2", "native_resolved", "closed", ("native",)),
             StaleEdge("o/r#1", "o/r#3", "prose_vanished", "open", ("prose",)),
             StaleEdge("o/r#4", "o/r#5", "derived_resolved", "closed", ("prose",))]
    out = h.resolved_edges("p", edges, {"o/r#1": "wi1"})
    assert [(a.ref, a.action, a.work_item_id, a.detail["blocker"]) for a in out] == [
        ("o/r#1<-o/r#2", "prune_edge", "wi1", "o/r#2"),
        ("o/r#1<-o/r#3", "prune_edge", "wi1", "o/r#3")]
    assert len({a.key for a in out}) == 2


# --- plan, caps, modes ------------------------------------------------------------------

def test_plan_runs_every_rule_and_caps_limit_writes_per_action():
    rows = [_row(n, status="Done") for n in range(1, 15)]
    rows += [_row(n, status="Blocked") for n in range(20, 45)]
    edges = [StaleEdge(f"o/r#{n}", "o/r#99", "native_resolved", "closed", ("native",))
             for n in range(100, 130)]
    actions = h.plan(_inputs(rows, board={r.work_item_id: OLD for r in rows}, edges=edges),
                     now=NOW)
    by = {a: sum(1 for x in actions if x.action == a) for a in h.ACTIONS}
    assert by == {"close": 14, "set_status": 25, "release_hold": 0, "prune_edge": 30,
                  "rewrite_hold": 0, "unpark": 0}
    capped = h.capped(actions)
    assert {a: sum(1 for x in capped if x.action == a) for a in h.ACTIONS} == {
        "close": 10, "set_status": 20, "release_hold": 0, "prune_edge": DEP_PRUNE_MAX,
        "rewrite_hold": 0, "unpark": 0}


@pytest.mark.parametrize("auto,product,expected", [
    ("on", "on", "on"), ("on", "dry_run", "dry_run"), ("dry_run", "on", "dry_run"),
    ("on", "off", "off"), ("off", "on", "off"), ("on", "bogus", "off")])
def test_effective_mode_is_the_lower_of_automation_and_product(auto, product, expected):
    assert h.effective_mode(auto, product) == expected


def test_risk_tolerance_decides_apply_versus_recommend():
    assert h.applies({"risk_tolerance": "bold"}) and h.applies({"risk_tolerance": "balanced"})
    assert not h.applies({"risk_tolerance": "cautious"}) and not h.applies({})


def test_sample_due_at_most_hourly():
    assert h.sample_due(None, NOW)
    assert not h.sample_due(NOW - timedelta(minutes=59), NOW)
    assert h.sample_due(NOW - timedelta(minutes=60), NOW)
    # A naive stored stamp (SQLite) reads as UTC.
    assert not h.sample_due((NOW - timedelta(minutes=5)).replace(tzinfo=None), NOW)


# --- rule 5: closeout_assess (design §3) --------------------------------------------------

def _closeout_row(number: int, **over: Any) -> QueueRow:
    from server.automations.backlog_github import closeout_hold_reason
    base: dict[str, Any] = dict(hold_kind="owner_action", hold_by="runner", body=f"b{number}",
                                hold_reason=closeout_hold_reason("the PR said `Refs`"))
    base.update(over)
    return _row(number, **base)


def _state(row: QueueRow, decision: str | None, text: str = "t", **over: Any) -> dict:
    from server.pm.outcomes import body_fingerprint
    return {"fingerprint": body_fingerprint(row.title, row.body), "decision": decision,
            "text": text, "summary": "s", "at": "x", "appliedAt": None, **over}


def test_closeout_pending_is_system_closeout_holds_without_a_current_assessment():
    from server.automations.backlog_github import LEGACY_CLOSEOUT_HOLD_REASON
    rows = [_closeout_row(1), _closeout_row(2), _closeout_row(3),
            _closeout_row(4, hold_reason=LEGACY_CLOSEOUT_HOLD_REASON),
            _closeout_row(5, hold_by="tony@example.com"),       # a person's: never
            _closeout_row(6, live_session=True),                  # somebody is on it
            _closeout_row(7, hold_reason="bounced at opus; fable needs approval"),
            _closeout_row(8, hold_by="automation:needs-you-hygiene",
                          hold_reason="Left for you:\n- [ ] x")]  # already narrowed
    states = {"o/r#2": _state(rows[1], "close"),
              # assessed an older body: stale, so pending again
              "o/r#3": {**_state(rows[2], "close"), "fingerprint": "old"}}
    pending = h.closeout_pending(h.ProductInputs(
        slug="p", project_id="pid", policy={}, rows=rows, board_updated_at={},
        stale_edges=[], closeout_states=states), now=NOW)
    assert [r.ref for r in pending] == ["o/r#1", "o/r#3", "o/r#4"]


def test_closeout_pending_skips_a_hold_rule_3_already_releases():
    rows = [_closeout_row(1, labels=("tracking",)), _closeout_row(2)]
    p = h.ProductInputs(slug="p", project_id="pid", policy={"skip_labels": ["tracking"]},
                        rows=rows, board_updated_at={}, stale_edges=[])
    assert [r.ref for r in h.closeout_pending(p, now=NOW)] == ["o/r#2"]
    assert [a.rule for a in h.plan(p, now=NOW)] == ["stale_holds"]


def test_closeout_assess_maps_each_decision_once_and_never_after_it_applied():
    rows = [_closeout_row(n) for n in range(1, 6)]
    states = {"o/r#1": _state(rows[0], "close", "comment"),
              "o/r#2": _state(rows[1], "release", "Left for an agent: add a test"),
              "o/r#3": _state(rows[2], "checklist", "Left for you:\n- [ ] prod check"),
              "o/r#4": _state(rows[3], None),                        # unusable reply
              "o/r#5": _state(rows[4], "close", appliedAt="2026-10-14T00:00:00Z")}
    out = h.closeout_assess(rows, states)
    assert [(a.ref, a.action) for a in out] == [
        ("o/r#1", "close"), ("o/r#2", "release_hold"), ("o/r#3", "rewrite_hold")]
    assert {a.rule for a in out} == {"closeout_assess"}
    assert out[0].detail["comment"] == "comment"
    assert out[2].detail["reason"] == "Left for you:\n- [ ] prod check"
    assert all(a.detail["holdReason"] == rows[0].hold_reason for a in out)
    p = h.ProductInputs(slug="p", project_id="pid", policy={}, rows=rows,
                        board_updated_at={}, stale_edges=[], closeout_states=states)
    assert [a.ref for a in h.plan(p, now=NOW)] == ["o/r#1", "o/r#2", "o/r#3"]
