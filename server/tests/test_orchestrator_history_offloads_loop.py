"""HistoryStore reads reached via the orchestrator must not block the event loop.

The P1 sibling audit classified `HistoryStore` as "fixed" because its constructor
and the pr_population ETA path were offloaded — but the store's OTHER callers,
`check_insights` and `all_repos_stage_models`, still queried it inline from
`async def` RPC/route handlers. Asserting scheduling latency (not the return
value) is the point: a result-only test is exactly what let RC1 ship.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from server.github.pr_status import PullRequestInfo
from server.sessions.orchestrator import SessionOrchestrator

BLOCK_S = 0.4


async def _ticker(stop: asyncio.Event, ticks: list[float]) -> None:
    """Records a timestamp every 10ms until told to stop."""
    while not stop.is_set():
        ticks.append(time.monotonic())
        await asyncio.sleep(0.01)


class _BlockingStore:
    """Stands in for a HistoryStore whose synchronous sqlite3 reads are slow."""

    def slowest_checks(self, repo, since_iso, limit=10, event=None):
        time.sleep(BLOCK_S)
        return []

    def flakiest_checks(self, repo, since_iso, limit=10, event=None):
        return []

    def duration_regressions(self, repo):
        return []

    def runner_wait_health(self, repo, since, prev_since):
        return []

    def ci_graph(self, repo):
        time.sleep(BLOCK_S)
        return None


class _Pop:
    def __init__(self, data=None):
        self._d = data or {}

    def all(self):
        return self._d

    def history_store(self):
        return _BlockingStore()


def _orch(population=None):
    orch = object.__new__(SessionOrchestrator)  # skip heavy __init__
    orch._pr_population = _Pop(population)
    orch._records = {}
    return orch


async def _assert_loop_kept_ticking(coro):
    stop = asyncio.Event()
    ticks: list[float] = []
    ticker = asyncio.create_task(_ticker(stop, ticks))

    started = time.monotonic()
    await coro
    elapsed = time.monotonic() - started

    stop.set()
    await ticker

    assert elapsed >= BLOCK_S, "the stub must actually block, else the test proves nothing"
    # Offloaded: ~elapsed/0.01 ticks. Run inline: the ticker is frozen, ~1 tick.
    assert len(ticks) > (elapsed / 0.02), (
        f"event loop was starved: only {len(ticks)} ticks in {elapsed:.2f}s"
    )


@pytest.mark.asyncio
async def test_check_insights_keeps_the_event_loop_responsive():
    await _assert_loop_kept_ticking(_orch().check_insights("o/r", "7d"))


@pytest.mark.asyncio
async def test_all_repos_stage_models_keeps_the_event_loop_responsive():
    pr = PullRequestInfo(number=1, url="u", state="open", checks="pending",
                         title="t", repo="o/r", stage="ci")
    orch = _orch({"o/r": (pr,)})
    await _assert_loop_kept_ticking(orch.all_repos_stage_models())


@pytest.mark.asyncio
async def test_all_prs_keeps_the_event_loop_responsive():
    """all_prs builds the stage models too — it must inherit the offload."""
    pr = PullRequestInfo(number=1, url="u", state="open", checks="pending",
                         title="t", repo="o/r", stage="ci")
    orch = _orch({"o/r": (pr,)})
    await _assert_loop_kept_ticking(orch.all_prs())
