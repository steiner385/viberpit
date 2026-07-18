"""Tests for the backlog grouping optimizer — plan parsing (pure), plan
consumption (pure), the Sonnet seam (canned ``_query_fn``; the real model is
NEVER called), and the plan store roundtrip (real SQLite)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from server.automations.backlog_grouping import (
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


def test_parse_appends_unplaced_candidates_as_singletons():
    raw = _plan_json([{"refs": ["o/r#9"], "rationale": "top"}])
    plan = parse_group_plan(raw, C, 3)
    assert plan[0]["refs"] == ["o/r#9"]
    # Leftovers in ascending issue-number order.
    assert [g["refs"] for g in plan[1:]] == [["o/r#2"], ["o/r#5"]]


def test_parse_garbage_degrades_to_all_singletons():
    """Unparseable model output → one issue per PR (the SAFE direction)."""
    plan = parse_group_plan("sorry, I cannot help with that", C, 3)
    assert [g["refs"] for g in plan] == [["o/r#2"], ["o/r#5"], ["o/r#9"]]


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
async def test_propose_groups_model_failure_returns_empty():
    def _query(*, prompt, options):  # noqa: ARG001
        raise RuntimeError("sdk down")

    assert await propose_groups(C, {"max_issues_per_pr": 2}, _query_fn=_query) == []


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
