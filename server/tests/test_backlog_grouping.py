"""Tests for the backlog grouping optimizer — plan parsing (pure), plan
consumption (pure), the Sonnet seam (canned ``_query_fn``; the real model is
NEVER called), and the plan store roundtrip (real SQLite)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from server.automations.backlog_grouping import (
    family_groups,
    float_urgent_first,
    order_by_pool_rank,
    parse_group_plan,
    plan_to_batch,
    propose_groups,
    store_plan,
    load_plan,
    mark_consumed,
)

C = [
    {"ref": "o/r#5", "number": 5, "repo": "o/r", "title": "Fix five",
     "body": "stack trace in auth"},
    {"ref": "o/r#2", "number": 2, "repo": "o/r", "title": "Fix two", "body": ""},
    {"ref": "o/r#9", "number": 9, "repo": "o/r", "title": "Fix nine", "body": None},
]


def _plan_json(groups: list[dict]) -> str:
    return json.dumps({"groups": groups})


# --------------------------------------------------------------------------- #
# parse_group_plan (pure)                                                      #
# --------------------------------------------------------------------------- #

def test_parse_valid_plan_preserves_groups_and_order():
    raw = _plan_json([
        {"refs": ["o/r#9", "o/r#5"], "rationale": "same subsystem"},
        {"refs": ["o/r#2"], "rationale": "standalone"},
    ])
    plan = parse_group_plan(raw, C, 3)
    assert plan == [
        {"refs": ["o/r#9", "o/r#5"], "rationale": "same subsystem"},
        {"refs": ["o/r#2"], "rationale": "standalone"},
    ]


def test_parse_strips_code_fences():
    raw = "```json\n" + _plan_json([{"refs": ["o/r#2"], "rationale": "r"}]) + "\n```"
    plan = parse_group_plan(raw, C, 2)
    assert plan[0] == {"refs": ["o/r#2"], "rationale": "r"}


def test_parse_drops_unknown_refs():
    raw = _plan_json([{"refs": ["o/x#1", "o/r#5"], "rationale": None}])
    plan = parse_group_plan(raw, C, 3)
    assert plan[0]["refs"] == ["o/r#5"]


def test_parse_groups_are_disjoint_first_wins():
    raw = _plan_json([
        {"refs": ["o/r#5", "o/r#2"]},
        {"refs": ["o/r#2", "o/r#9"]},  # #2 already placed → dropped here
    ])
    plan = parse_group_plan(raw, C, 3)
    assert plan[0]["refs"] == ["o/r#5", "o/r#2"]
    assert plan[1]["refs"] == ["o/r#9"]


def test_parse_truncates_oversize_groups_to_max_k():
    """max_issues_per_pr is a HARD CAP the model cannot exceed — the exact
    invariant the old rank-and-slice path inverted into a target."""
    raw = _plan_json([{"refs": ["o/r#2", "o/r#5", "o/r#9"], "rationale": "all"}])
    plan = parse_group_plan(raw, C, 2)
    assert plan[0]["refs"] == ["o/r#2", "o/r#5"]
    # The truncated ref is re-covered as a singleton, not lost.
    assert {"refs": ["o/r#9"], "rationale": None} in plan


def test_parse_appends_unplaced_candidates_in_the_ranked_order_they_arrived():
    """Leftovers keep the POOL's order, which is the queue's ranking.

    ``candidates`` arrives sorted by ``server.pm.queue.rank_key``. Re-sorting
    leftovers by issue number here was a second definition of priority, in a
    consumer, that outranked the canonical one — see the module comment.
    """
    raw = _plan_json([{"refs": ["o/r#9"], "rationale": "top"}])
    plan = parse_group_plan(raw, C, 3)
    assert plan[0]["refs"] == ["o/r#9"]
    assert [g["refs"] for g in plan[1:]] == [["o/r#5"], ["o/r#2"]]


def test_parse_garbage_degrades_to_all_singletons_still_in_ranked_order():
    """Unparseable model output → one issue per PR (the SAFE direction), and
    still in the queue's order.

    This is the path the live runner actually takes: every group in the
    production plan has a null rationale, i.e. the model places nothing and the
    whole pool falls through here. So this ordering, not the model's, is what
    decides what gets worked on.
    """
    plan = parse_group_plan("sorry, I cannot help with that", C, 3)
    assert [g["refs"] for g in plan] == [["o/r#5"], ["o/r#2"], ["o/r#9"]]


def test_leftover_order_preserves_score_and_age_not_just_urgency():
    """#1698 floated urgent groups to the front, which fixes the incident case.

    The cause was wider: the number re-sort discarded the ranking for EVERY
    row, so a groomed, high-score issue still lost its place to whichever issue
    happened to be filed first. Here the pool is ranked #900 → #100 (a human
    scored #900 above), and issue-number order would exactly invert it.
    """
    ranked = [
        {"ref": "o/r#900", "number": 900, "repo": "o/r", "title": "scored top", "body": ""},
        {"ref": "o/r#100", "number": 100, "repo": "o/r", "title": "unscored", "body": ""},
    ]
    plan = parse_group_plan("not json", ranked, 3)
    assert [g["refs"] for g in plan] == [["o/r#900"], ["o/r#100"]]


def test_parse_accepts_top_level_list():
    plan = parse_group_plan(json.dumps([{"refs": ["o/r#5"]}]), C, 2)
    assert plan[0]["refs"] == ["o/r#5"]


# --------------------------------------------------------------------------- #
# plan_to_batch (pure)                                                         #
# --------------------------------------------------------------------------- #

def test_plan_to_batch_pops_first_live_group():
    plan = [{"id": 1, "refs": ["o/r#9", "o/r#5"]}, {"id": 2, "refs": ["o/r#2"]}]
    batch, gid = plan_to_batch(plan, C)
    assert gid == 1
    assert batch is not None
    assert batch.primary == "o/r#9"
    assert batch.issues == ["o/r#9", "o/r#5"]
    assert batch.repo == "o/r"


def test_plan_to_batch_skips_fully_stale_groups():
    plan = [{"id": 1, "refs": ["o/r#77"]}, {"id": 2, "refs": ["o/r#2"]}]
    batch, gid = plan_to_batch(plan, C)
    assert gid == 2 and batch is not None and batch.primary == "o/r#2"


def test_plan_to_batch_carries_the_primarys_hold_kind():
    """Spec 2026-09-18 §5: the dispatch KIND is the primary's hold, and the
    candidate dict is all that crosses this hand-off — so the hold rides onto the
    batch, and a SECONDARY's hold does not (the session is for the primary)."""
    cands = [
        {"ref": "o/r#9", "number": 9, "repo": "o/r", "title": "a", "hold_kind": "evidence"},
        {"ref": "o/r#5", "number": 5, "repo": "o/r", "title": "b", "hold_kind": "owner_action"},
        {"ref": "o/r#2", "number": 2, "repo": "o/r", "title": "c"},
    ]
    held, _ = plan_to_batch([{"id": 1, "refs": ["o/r#9", "o/r#5"]}], cands)
    assert held is not None and held.primary_hold_kind == "evidence"
    unheld, _ = plan_to_batch([{"id": 2, "refs": ["o/r#2"]}], cands)
    assert unheld is not None and unheld.primary_hold_kind is None


def test_plan_to_batch_empty_plan_waits():
    assert plan_to_batch([], C) == (None, None)


def test_plan_to_batch_fairness_prefers_light_repo():
    cands = [
        {"ref": "o/a#1", "number": 1, "repo": "o/a", "title": "a"},
        {"ref": "o/b#2", "number": 2, "repo": "o/b", "title": "b"},
    ]
    plan = [{"id": 1, "refs": ["o/a#1"]}, {"id": 2, "refs": ["o/b#2"]}]
    batch, gid = plan_to_batch(plan, cands, {"o/a": 3, "o/b": 0})
    assert gid == 2 and batch is not None and batch.repo == "o/b"


# --------------------------------------------------------------------------- #
# propose_groups (canned SDK seam)                                             #
# --------------------------------------------------------------------------- #

def _canned_query_fn(reply: str, calls: list[dict] | None = None):
    async def _gen(*, prompt: str, options):
        if calls is not None:
            calls.append({"prompt": prompt, "options": options})
        yield SimpleNamespace(content=[SimpleNamespace(text=reply)])

    def _query(*, prompt: str, options):
        return _gen(prompt=prompt, options=options)

    return _query


@pytest.mark.asyncio
async def test_propose_groups_parses_model_reply():
    reply = _plan_json([{"refs": ["o/r#9", "o/r#5"], "rationale": "related"}])
    calls: list[dict] = []
    plan = await propose_groups(C, {"max_issues_per_pr": 2},
                                _query_fn=_canned_query_fn(reply, calls))
    assert plan[0] == {"refs": ["o/r#9", "o/r#5"], "rationale": "related"}
    # The prompt carries titles AND body excerpts, and the call runs on Sonnet.
    assert "Fix nine" in calls[0]["prompt"]
    assert "stack trace in auth" in calls[0]["prompt"]
    assert calls[0]["options"].model == "sonnet"


@pytest.mark.asyncio
async def test_propose_groups_empty_candidates_no_model_call():
    called = False

    def _query(**_kw):
        nonlocal called
        called = True
        raise AssertionError("must not be called")

    assert await propose_groups([], {}, _query_fn=_query) == []
    assert not called


@pytest.mark.asyncio
async def test_propose_groups_model_failure_raises_with_the_models_error():
    """A model-call failure surfaces as ``GroupingModelError`` carrying the SDK's
    own message, so the runner's ``groups_refresh_failed`` event can name it
    (2026-09-16: "Claude Code returned an error result: success" was in the
    journal only; the event said "retrying shortly"). An unparseable plan is a
    different thing and still reads as an empty plan."""
    from server.automations.backlog_grouping import GroupingModelError

    def _query(*, prompt, options):  # noqa: ARG001
        raise RuntimeError("sdk down")

    with pytest.raises(GroupingModelError, match="sdk down"):
        await propose_groups(C, {"max_issues_per_pr": 2}, _query_fn=_query)


# --------------------------------------------------------------------------- #
# Plan store roundtrip (real SQLite)                                           #
# --------------------------------------------------------------------------- #

def test_store_load_consume_roundtrip(tmp_path):
    from server.db import init_db

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'plan.db'}")
        n = await store_plan([
            {"refs": ["o/r#9", "o/r#5"], "rationale": "related"},
            {"refs": ["o/r#2"], "rationale": None},
        ], C)
        assert n == 2
        plan = await load_plan()
        assert [g["refs"] for g in plan] == [["o/r#9", "o/r#5"], ["o/r#2"]]
        assert plan[0]["repo"] == "o/r"
        assert plan[0]["titles"] == {"o/r#9": "Fix nine", "o/r#5": "Fix five"}
        assert plan[0]["rationale"] == "related"

        # Consuming a group hides it from the next load (UI keeps the row).
        await mark_consumed(plan[0]["id"])
        assert [g["refs"] for g in await load_plan()] == [["o/r#2"]]

        # A refresh REPLACES the whole generation, consumed rows included.
        await store_plan([{"refs": ["o/r#2"], "rationale": None}], C)
        left = await load_plan()
        assert [g["refs"] for g in left] == [["o/r#2"]]

        # An empty backlog stores an empty plan.
        await store_plan([], [])
        assert await load_plan() == []

    asyncio.run(go())


# ── urgency: the plan must not bury an incident (#16150, #16151) ─────────────

_URGENT = [
    {"ref": "o/r#2", "number": 2, "repo": "o/r", "title": "Fix two", "body": ""},
    {"ref": "o/r#5", "number": 5, "repo": "o/r", "title": "Fix five", "body": ""},
    {"ref": "o/r#900", "number": 900, "repo": "o/r", "body": "",
     "title": "[deploy-monitor] prod deploy failing", "urgent": True},
]


def test_parse_floats_an_urgent_group_to_the_front_of_a_model_plan():
    """Ranking the pool is not enough: the model re-orders it into the plan, and
    the runner pops the plan. So the same invariant has to hold on this side of
    the hand-off — otherwise a model that ranks the incident last (which is what
    happened: 328th of 332) silently undoes the queue's urgency leg."""
    raw = _plan_json([{"refs": ["o/r#2"], "rationale": "a"},
                      {"refs": ["o/r#5"], "rationale": "b"},
                      {"refs": ["o/r#900"], "rationale": "c"}])
    plan = parse_group_plan(raw, _URGENT, 1)
    assert [g["refs"] for g in plan] == [["o/r#900"], ["o/r#2"], ["o/r#5"]]


def test_parse_floats_an_urgent_leftover_too():
    """The degraded path is the one that actually bit: unparseable output becomes
    all-singletons in ISSUE-NUMBER order, and an incident is the newest issue
    there is — so it sorts last, every single refresh."""
    plan = parse_group_plan("not json at all", _URGENT, 1)
    assert plan[0]["refs"] == ["o/r#900"]


def test_parse_keeps_a_bundled_urgent_group_whole_and_first():
    raw = _plan_json([{"refs": ["o/r#2"], "rationale": "a"},
                      {"refs": ["o/r#5", "o/r#900"], "rationale": "same break"}])
    plan = parse_group_plan(raw, _URGENT, 2)
    assert [g["refs"] for g in plan] == [["o/r#5", "o/r#900"], ["o/r#2"]]


def test_parse_preserves_relative_order_among_equally_urgent_groups():
    # Stable: floating urgent work must not otherwise reshuffle the model's
    # priority ordering, or the plan stops meaning what the model said.
    # o/r#9 is unplaced by this plan, so it trails as a leftover singleton — the
    # model's own #5-before-#2 ordering is what must survive untouched.
    plan = parse_group_plan(_plan_json([{"refs": ["o/r#5"], "rationale": "b"},
                                        {"refs": ["o/r#2"], "rationale": "a"}]), C, 1)
    assert [g["refs"] for g in plan] == [["o/r#5"], ["o/r#2"], ["o/r#9"]]


# --------------------------------------------------------------------------- #
# family_groups (pure): sibling error reports become one investigation         #
# --------------------------------------------------------------------------- #

def _evidence(ref: str, family: str | None) -> dict:
    return {"ref": ref, "number": int(ref.rpartition("#")[2]), "repo": "o/r",
            "title": f"[auto-error] {ref}", "body": "", "hold_kind": "evidence",
            "family": family}


def _plain(ref: str) -> dict:
    return {"ref": ref, "number": int(ref.rpartition("#")[2]), "repo": "o/r",
            "title": f"Fix {ref}", "body": "", "hold_kind": None}


def test_family_groups_bundles_siblings_and_overflows_at_max_k():
    candidates = [
        _evidence("o/r#1", "server.a:boom"),
        _evidence("o/r#2", "server.b:crash"),
        _evidence("o/r#3", "server.a:boom"),
        _evidence("o/r#4", "server.a:boom"),
        _plain("o/r#5"),
        _evidence("o/r#6", "server.b:crash"),
        _plain("o/r#7"),
    ]

    groups, rest = family_groups(candidates, max_k=2)

    assert [g["refs"] for g in groups] == [
        ["o/r#1", "o/r#3"],   # family A, capped at max_k…
        ["o/r#4"],            # …and its overflow, still family A
        ["o/r#2", "o/r#6"],   # family B, after A because #1 outranks #2
    ]
    assert [c["ref"] for c in rest] == ["o/r#5", "o/r#7"]


def test_family_groups_name_the_family_in_the_rationale():
    groups, _ = family_groups([_evidence("o/r#1", "server.a:boom"),
                               _evidence("o/r#3", "server.a:boom")], max_k=5)
    assert len(groups) == 1
    assert "server.a:boom" in (groups[0]["rationale"] or "")


def test_family_groups_keep_familyless_evidence_apart():
    """No family key means no evidence of relatedness — the same rule the model
    prompt states. Two unrelated reports must not merge just by both lacking one."""
    groups, rest = family_groups(
        [_evidence("o/r#1", None), _evidence("o/r#2", None)], max_k=5)

    assert [g["refs"] for g in groups] == [["o/r#1"], ["o/r#2"]]
    assert rest == []


def test_family_groups_pass_non_evidence_holds_through_to_the_model():
    """Only an ``evidence`` hold dispatches as an investigation; a stronger-model
    hold is still ordinary implement work and stays in the model's pool."""
    candidates = [{**_plain("o/r#1"), "hold_kind": "stronger_model"},
                  _plain("o/r#2")]

    groups, rest = family_groups(candidates, max_k=2)

    assert groups == []
    assert [c["ref"] for c in rest] == ["o/r#1", "o/r#2"]


def test_family_groups_treat_max_k_below_one_as_one():
    groups, _ = family_groups([_evidence("o/r#1", "f"), _evidence("o/r#2", "f")],
                              max_k=0)
    assert [g["refs"] for g in groups] == [["o/r#1"], ["o/r#2"]]


def test_family_groups_produce_the_shape_store_plan_persists(tmp_path):
    """The evidence groups are concatenated with the model's plan and stored by the
    same ``store_plan``, so they must be the same dict shape — not a second one."""
    from server.db import init_db

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'fam-plan.db'}")
        candidates = [_evidence("o/r#1", "server.a:boom"),
                      _evidence("o/r#3", "server.a:boom")]
        groups, _ = family_groups(candidates, max_k=2)
        assert await store_plan(groups, candidates) == 1
        plan = await load_plan()
        assert plan[0]["refs"] == ["o/r#1", "o/r#3"]
        assert plan[0]["repo"] == "o/r"
        assert plan[0]["titles"] == {"o/r#1": "[auto-error] o/r#1",
                                     "o/r#3": "[auto-error] o/r#3"}

    asyncio.run(run())


def test_float_urgent_first_leads_with_the_urgent_group_and_is_stable():
    """The evidence half is assembled outside ``parse_group_plan``, so the urgent
    float has to be applicable to an already-built plan — one definition, two
    callers."""
    plan = [{"refs": ["o/r#1", "o/r#3"], "rationale": "family"},
            {"refs": ["o/r#4"], "rationale": "family overflow"},
            {"refs": ["o/r#900"], "rationale": "model"}]
    candidates = [_evidence("o/r#1", "f"), _evidence("o/r#3", "f"),
                  _evidence("o/r#4", "f"),
                  {**_plain("o/r#900"), "urgent": True}]

    assert [g["refs"] for g in float_urgent_first(plan, candidates)] == [
        ["o/r#900"], ["o/r#1", "o/r#3"], ["o/r#4"]]


# --------------------------------------------------------------------------- #
# order_by_pool_rank (pure): the two halves interleave, never concatenate       #
# --------------------------------------------------------------------------- #

def test_order_by_pool_rank_interleaves_the_evidence_and_model_halves():
    """Spec 2026-09-18 §5 dispatches an evidence hold as an ordinary candidate —
    ranking included. Concatenating the halves made the concatenation itself the
    priority decision: every auto-filed report led every implement group. Each
    group takes its best member's rank in the (already rank-ordered) pool instead."""
    candidates = [
        _plain("o/r#100"),                     # rank 0
        _evidence("o/r#201", None),            # rank 1
        _plain("o/r#101"),                     # rank 2
        _evidence("o/r#202", None),            # rank 3
        _plain("o/r#102"),                     # rank 4
        _evidence("o/r#203", None),            # rank 5
    ]
    # Three evidence singletons (ranks 1, 3, 5) in front of two model groups whose
    # best members are ranks 0 and 2 — the concatenated order.
    plan = [{"refs": ["o/r#201"]}, {"refs": ["o/r#202"]}, {"refs": ["o/r#203"]},
            {"refs": ["o/r#100", "o/r#102"]}, {"refs": ["o/r#101"]}]

    assert [g["refs"] for g in order_by_pool_rank(plan, candidates)] == [
        ["o/r#100", "o/r#102"],   # rank 0 (best member of the bundle)
        ["o/r#201"],              # rank 1
        ["o/r#101"],              # rank 2
        ["o/r#202"],              # rank 3
        ["o/r#203"],              # rank 5
    ]


def test_order_by_pool_rank_sorts_a_group_with_no_live_ref_last():
    """A group whose refs all left the pool has no rank to take. It must sort LAST
    — a missing key that defaulted to 0 would put stale work at the head of the
    plan, ahead of everything the queue actually ranked."""
    candidates = [_plain("o/r#1"), _plain("o/r#2")]
    plan = [{"refs": ["o/r#404"]}, {"refs": ["o/r#2"]}, {"refs": ["o/r#1"]}]

    assert [g["refs"] for g in order_by_pool_rank(plan, candidates)] == [
        ["o/r#1"], ["o/r#2"], ["o/r#404"]]


def test_order_by_pool_rank_then_urgent_float_keeps_urgent_first():
    """The two orderings compose in one direction only: rank first, urgency on top.
    An incident is ranked first by the queue anyway, but the float is what
    GUARANTEES it — and it must survive the rank sort, not be undone by it."""
    candidates = [_evidence("o/r#1", "f"),
                  {**_plain("o/r#900"), "urgent": True},
                  _evidence("o/r#2", "f")]
    plan = [{"refs": ["o/r#1"]}, {"refs": ["o/r#2"]}, {"refs": ["o/r#900"]}]

    ranked = order_by_pool_rank(plan, candidates)
    assert [g["refs"] for g in ranked] == [["o/r#1"], ["o/r#900"], ["o/r#2"]]
    assert [g["refs"] for g in float_urgent_first(ranked, candidates)] == [
        ["o/r#900"], ["o/r#1"], ["o/r#2"]]
