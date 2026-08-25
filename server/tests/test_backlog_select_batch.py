"""Tests for select_batch (query live candidates → pop a precomputed plan group).

All I/O seams are injected — canned query/plan-store functions — so no DB, no
network, and no model is ever touched. The runner-side invariant under test:
selection NEVER invents a grouping; it only consumes what the background
grouping optimizer planned, intersected with the live candidate pool.
"""
from __future__ import annotations

import pytest

from server.automations.backlog_runner import select_batch
from server.automations.backlog_selection import Batch
from server.tests.fakes.fake_backlog import FakeGh


# --------------------------------------------------------------------------- #
# Helpers / fixtures                                                           #
# --------------------------------------------------------------------------- #

CANDIDATES = [
    {"ref": "o/r#5", "number": 5, "repo": "o/r", "title": "Fix five"},
    {"ref": "o/r#2", "number": 2, "repo": "o/r", "title": "Fix two"},
    {"ref": "o/r#9", "number": 9, "repo": "o/r", "title": "Fix nine"},
]


def make_config(**overrides) -> dict:
    cfg = {
        "enabled_repos": ["o/r"],
        "eligible_label": "ready",
        "max_concurrent": 1,
        "max_spawns_per_day": 10,
        "max_attempts": 2,
        "max_issues_per_pr": 1,
    }
    cfg.update(overrides)
    return cfg


def canned_query(candidates: list[dict]):
    """Return a query function that always yields *candidates*, ignoring args."""
    def _query(config: dict, *, gh_run=None) -> list[dict]:  # noqa: ARG001
        return list(candidates)
    return _query


def canned_plan(groups: list[dict]):
    """Return an async load_plan seam serving canned plan groups."""
    async def _load() -> list[dict]:
        return [dict(g) for g in groups]
    return _load


def consumed_recorder(into: list[int]):
    async def _mark(group_id: int) -> None:
        into.append(group_id)
    return _mark


# --------------------------------------------------------------------------- #
# Plan consumption                                                             #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_select_batch_pops_highest_priority_group():
    """The first (rank-0) plan group becomes the batch, primary first."""
    consumed: list[int] = []
    result = await select_batch(
        make_config(),
        query=canned_query(CANDIDATES),
        load_plan=canned_plan([
            {"id": 1, "refs": ["o/r#9", "o/r#5"], "repo": "o/r"},
            {"id": 2, "refs": ["o/r#2"], "repo": "o/r"},
        ]),
        mark_consumed=consumed_recorder(consumed),
    )
    assert isinstance(result, Batch)
    assert result.primary == "o/r#9"
    assert result.issues == ["o/r#9", "o/r#5"]
    assert result.repo == "o/r"
    assert result.titles == {"o/r#9": "Fix nine", "o/r#5": "Fix five"}
    assert consumed == [1]


@pytest.mark.asyncio
async def test_select_batch_drops_stale_refs_from_group():
    """Refs no longer in the live pool (claimed/closed since planning) are
    dropped from the batch — never re-invented, never replaced."""
    result = await select_batch(
        make_config(),
        query=canned_query([CANDIDATES[1]]),  # only #2 still live
        load_plan=canned_plan([{"id": 7, "refs": ["o/r#9", "o/r#2"], "repo": "o/r"}]),
        mark_consumed=consumed_recorder([]),
    )
    assert result is not None
    assert result.primary == "o/r#2"
    assert result.issues == ["o/r#2"]


@pytest.mark.asyncio
async def test_select_batch_waits_when_plan_is_empty():
    """Candidates exist but no plan → None. The runner WAITS for the grouper
    instead of choosing issues itself (the core behavior of this feature)."""
    result = await select_batch(
        make_config(),
        query=canned_query(CANDIDATES),
        load_plan=canned_plan([]),
        mark_consumed=consumed_recorder([]),
    )
    assert result is None


@pytest.mark.asyncio
async def test_select_batch_waits_when_all_groups_stale():
    """A plan whose every ref has left the live pool is dead weight → None."""
    result = await select_batch(
        make_config(),
        query=canned_query(CANDIDATES),
        load_plan=canned_plan([{"id": 3, "refs": ["o/r#77"], "repo": "o/r"}]),
        mark_consumed=consumed_recorder([]),
    )
    assert result is None


@pytest.mark.asyncio
async def test_select_batch_empty_candidates_returns_none_without_plan_read():
    """No candidates → None; the plan store must not even be read."""
    plan_read = False

    async def strict_load() -> list[dict]:
        nonlocal plan_read
        plan_read = True
        return []

    result = await select_batch(
        make_config(),
        query=canned_query([]),
        load_plan=strict_load,
        mark_consumed=consumed_recorder([]),
    )
    assert result is None
    assert not plan_read, "load_plan must not be called when candidates are empty"


@pytest.mark.asyncio
async def test_select_batch_mark_consumed_failure_does_not_block_spawn():
    """A failing consumed-stamp (UI-only bookkeeping) must not lose the batch."""
    async def broken_mark(_gid: int) -> None:
        raise RuntimeError("db hiccup")

    result = await select_batch(
        make_config(),
        query=canned_query(CANDIDATES),
        load_plan=canned_plan([{"id": 1, "refs": ["o/r#2"], "repo": "o/r"}]),
        mark_consumed=broken_mark,
    )
    assert result is not None and result.primary == "o/r#2"


# --------------------------------------------------------------------------- #
# Per-repo fairness (repo_load)                                                #
# --------------------------------------------------------------------------- #

_MULTI_REPO = [
    {"ref": "o/a#1", "number": 1, "repo": "o/a", "title": "a-one"},
    {"ref": "o/b#2", "number": 2, "repo": "o/b", "title": "b-two"},
]

_MULTI_PLAN = [
    {"id": 1, "refs": ["o/a#1"], "repo": "o/a"},
    {"id": 2, "refs": ["o/b#2"], "repo": "o/b"},
]


@pytest.mark.asyncio
async def test_select_batch_prefers_least_loaded_repo():
    """Plan priority puts o/a first, but o/a already has active runs and o/b has
    none → fairness re-biases the pick to the idle repo o/b."""
    result = await select_batch(
        make_config(enabled_repos=["o/a", "o/b"]),
        query=canned_query(_MULTI_REPO),
        load_plan=canned_plan(_MULTI_PLAN),
        mark_consumed=consumed_recorder([]),
        repo_load={"o/a": 3, "o/b": 0},
    )
    assert result is not None
    assert result.primary == "o/b#2"
    assert result.repo == "o/b"


@pytest.mark.asyncio
async def test_repo_load_preserves_plan_priority_within_a_repo():
    """Fairness is a stable re-ordering: within one repo, plan order stands."""
    result = await select_batch(
        make_config(),
        query=canned_query(CANDIDATES),
        load_plan=canned_plan([
            {"id": 1, "refs": ["o/r#9"], "repo": "o/r"},
            {"id": 2, "refs": ["o/r#2"], "repo": "o/r"},
        ]),
        mark_consumed=consumed_recorder([]),
        repo_load={"o/r": 5},
    )
    assert result is not None
    assert result.primary == "o/r#9"  # single repo → plan priority unchanged


@pytest.mark.asyncio
async def test_no_repo_load_keeps_plan_order():
    """Absent repo_load (the default) leaves selection exactly the plan order."""
    result = await select_batch(
        make_config(enabled_repos=["o/a", "o/b"]),
        query=canned_query(_MULTI_REPO),
        load_plan=canned_plan(_MULTI_PLAN),
        mark_consumed=consumed_recorder([]),
    )
    assert result is not None
    assert result.primary == "o/a#1"  # plan's top group, no fairness applied


# --------------------------------------------------------------------------- #
# Selection strategy read from config (selection_strategy + repo_weights/caps)  #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_strategy_priority_serves_by_enabled_repo_order():
    """'priority' ranks repos by the enabled_repos order — o/b first here even
    though the plan puts o/a's group first."""
    result = await select_batch(
        make_config(enabled_repos=["o/b", "o/a"], selection_strategy="priority"),
        query=canned_query(_MULTI_REPO),
        load_plan=canned_plan(_MULTI_PLAN),
        mark_consumed=consumed_recorder([]),
    )
    assert result is not None and result.primary == "o/b#2"


@pytest.mark.asyncio
async def test_strategy_weighted_favors_higher_weight():
    """'weighted' compares load/weight: o/a 6/3=2.0 beats o/b 3/1=3.0."""
    result = await select_batch(
        make_config(enabled_repos=["o/a", "o/b"], selection_strategy="weighted",
                    repo_weights={"o/a": 3, "o/b": 1}),
        query=canned_query(_MULTI_REPO),
        load_plan=canned_plan(_MULTI_PLAN),
        mark_consumed=consumed_recorder([]),
        repo_load={"o/a": 6, "o/b": 3},
    )
    assert result is not None and result.primary == "o/a#1"


@pytest.mark.asyncio
async def test_strategy_concurrency_cap_skips_capped_repo():
    """A repo at its cap is skipped; the under-cap repo takes the slot."""
    result = await select_batch(
        make_config(enabled_repos=["o/a", "o/b"],
                    selection_strategy="concurrency-cap", repo_caps={"o/a": 2}),
        query=canned_query(_MULTI_REPO),
        load_plan=canned_plan(_MULTI_PLAN),
        mark_consumed=consumed_recorder([]),
        repo_load={"o/a": 2, "o/b": 0},
    )
    assert result is not None and result.primary == "o/b#2"


@pytest.mark.asyncio
async def test_strategy_concurrency_cap_idles_when_all_capped():
    """Every repo with work is at its cap → no batch (the slot idles rather than
    exceeding a hard ceiling)."""
    result = await select_batch(
        make_config(enabled_repos=["o/a", "o/b"],
                    selection_strategy="concurrency-cap",
                    repo_caps={"o/a": 2, "o/b": 1}),
        query=canned_query(_MULTI_REPO),
        load_plan=canned_plan(_MULTI_PLAN),
        mark_consumed=consumed_recorder([]),
        repo_load={"o/a": 2, "o/b": 1},
    )
    assert result is None


# --------------------------------------------------------------------------- #
# FakeGh wiring (gh_run injected)                                              #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_select_batch_passes_gh_run_to_query():
    """gh_run is forwarded to the query callable; FakeGh records the call."""
    gh = FakeGh()
    received_gh_run = None

    def gh_aware_query(config: dict, *, gh_run=None) -> list[dict]:
        nonlocal received_gh_run
        received_gh_run = gh_run
        return list(CANDIDATES)

    await select_batch(
        make_config(),
        query=gh_aware_query,
        gh_run=gh,
        load_plan=canned_plan([{"id": 1, "refs": ["o/r#2"], "repo": "o/r"}]),
        mark_consumed=consumed_recorder([]),
    )
    assert received_gh_run is gh
