"""Tests for select_batch (query → rank → batch assembly seam).

All I/O seams are injected — FakeGh for the gh runner, canned rank functions
for the LLM ranking — so the real model is NEVER called in these tests.
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


# --------------------------------------------------------------------------- #
# Basic assembly                                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_select_batch_returns_batch_with_ranked_primary():
    """Canned rank [#9, #5, #2] → primary is #9."""
    config = make_config(max_issues_per_pr=1)
    result = await select_batch(
        config,
        query=canned_query(CANDIDATES),
        rank=lambda cs, cfg: ["o/r#9", "o/r#5", "o/r#2"],
    )
    assert isinstance(result, Batch)
    assert result.primary == "o/r#9"
    assert result.issues == ["o/r#9"]
    assert result.repo == "o/r"


@pytest.mark.asyncio
async def test_select_batch_bundles_multiple_issues():
    """max_issues_per_pr=2 with canned rank → top two in batch."""
    config = make_config(max_issues_per_pr=2)
    result = await select_batch(
        config,
        query=canned_query(CANDIDATES),
        rank=lambda cs, cfg: ["o/r#9", "o/r#5", "o/r#2"],
    )
    assert result is not None
    assert result.primary == "o/r#9"
    assert result.issues == ["o/r#9", "o/r#5"]


@pytest.mark.asyncio
async def test_select_batch_empty_candidates_returns_none():
    """No candidates → None (no crash, no model call)."""
    config = make_config()
    real_model_called = False

    def strict_rank(cs, cfg):
        nonlocal real_model_called
        real_model_called = True
        return []

    result = await select_batch(
        config,
        query=canned_query([]),
        rank=strict_rank,
    )
    assert result is None
    assert not real_model_called, "rank must not be called when candidates are empty"


@pytest.mark.asyncio
async def test_select_batch_malformed_rank_falls_back_to_lowest_number():
    """rank returns bogus refs → rank_and_batch fallback: lowest number first."""
    config = make_config(max_issues_per_pr=1)
    result = await select_batch(
        config,
        query=canned_query(CANDIDATES),
        rank=lambda cs, cfg: ["bogus-ref", "also-bad"],
    )
    assert result is not None
    assert result.primary == "o/r#2"  # deterministic fallback: lowest number


@pytest.mark.asyncio
async def test_select_batch_rank_never_called_when_single_candidate():
    """With one candidate, rank_and_batch skips the ranking call entirely."""
    config = make_config(max_issues_per_pr=1)
    rank_called = False

    def strict_rank(cs, cfg):
        nonlocal rank_called
        rank_called = True
        raise AssertionError("rank must not be called for a single candidate")

    result = await select_batch(
        config,
        query=canned_query([CANDIDATES[0]]),
        rank=strict_rank,
    )
    assert result is not None
    assert result.primary == "o/r#5"
    assert not rank_called


# --------------------------------------------------------------------------- #
# FakeGh wiring (gh_run injected)                                             #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_select_batch_passes_gh_run_to_query():
    """gh_run is forwarded to the query callable; FakeGh records the call."""
    gh = FakeGh()
    gh_called = False
    received_gh_run = None

    def gh_aware_query(config: dict, *, gh_run=None) -> list[dict]:
        nonlocal gh_called, received_gh_run
        gh_called = True
        received_gh_run = gh_run
        return list(CANDIDATES)

    config = make_config()
    await select_batch(
        config,
        query=gh_aware_query,
        rank=lambda cs, cfg: [cs[0]["ref"]],
        gh_run=gh,
    )
    assert gh_called
    assert received_gh_run is gh


# --------------------------------------------------------------------------- #
# Real model is never called                                                   #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_real_model_never_called():
    """Injected rank function is used; real SDK query is never invoked."""
    config = make_config(max_issues_per_pr=1)
    rank_calls: list[list[dict]] = []

    def recorder(cs, cfg):
        rank_calls.append(list(cs))
        return [cs[0]["ref"]]

    result = await select_batch(
        config,
        query=canned_query(CANDIDATES),
        rank=recorder,
    )
    assert result is not None
    # The injected rank was used (not the real model)
    assert len(rank_calls) == 1
    assert rank_calls[0] == CANDIDATES


# --------------------------------------------------------------------------- #
# Config forwarded to rank                                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_config_forwarded_to_rank():
    """select_batch passes config to rank so rank_issues can read max_issues_per_pr."""
    received_config = {}

    def config_recorder(cs, cfg):
        received_config.update(cfg)
        return [cs[0]["ref"]]

    config = make_config(max_issues_per_pr=3)
    await select_batch(
        config,
        query=canned_query(CANDIDATES),
        rank=config_recorder,
    )
    assert received_config.get("max_issues_per_pr") == 3
