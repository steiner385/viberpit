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
    assert by == {"close": 14, "set_status": 25, "release_hold": 0, "prune_edge": 30}
    capped = h.capped(actions)
    assert {a: sum(1 for x in capped if x.action == a) for a in h.ACTIONS} == {
        "close": 10, "set_status": 20, "release_hold": 0, "prune_edge": DEP_PRUNE_MAX}


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
