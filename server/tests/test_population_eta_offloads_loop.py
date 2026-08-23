"""ETA enrichment must not block the event loop (RC1).

The blocking half lives in the module-level _enrich_sync. We replace it with one
that blocks for a known duration: run inline it freezes every other task; run via
asyncio.to_thread the loop keeps ticking. Asserting scheduling latency — not the
return value — is the point, because a result-only test is what let RC1 ship.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from server.github import pr_population
from server.github.pr_population import PrPopulationPoller
from server.tests.test_population_eta_wired import (
    NOW,
    REPO,
    _build_db,
    _eta_checks_resp_empty,
    _pop_resp,
)


async def _ticker(stop: asyncio.Event, ticks: list[float]) -> None:
    """Records a timestamp every 10ms until told to stop."""
    while not stop.is_set():
        ticks.append(time.monotonic())
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_enrichment_keeps_the_event_loop_responsive(monkeypatch, tmp_path):
    BLOCK_S = 0.4

    def _blocking_enrich(*args, **kwargs):
        time.sleep(BLOCK_S)          # stands in for many blocking sqlite3 reads
        return {}

    monkeypatch.setattr(pr_population, "_enrich_sync", _blocking_enrich)

    store = _build_db(tmp_path / "ph.db")
    poller = PrPopulationPoller(
        query_fn=lambda repo: _pop_resp(42),
        eta_query_fn=lambda prs: _eta_checks_resp_empty(prs),
        history_store=store,
        now_fn=lambda: NOW,
    )
    prs_by_repo = {REPO: tuple(pr_population.parse_population_response(
        _pop_resp(42), REPO
    ))}

    stop = asyncio.Event()
    ticks: list[float] = []
    ticker = asyncio.create_task(_ticker(stop, ticks))

    started = time.monotonic()
    await poller._enrich_with_eta(prs_by_repo)
    elapsed = time.monotonic() - started

    stop.set()
    await ticker

    assert elapsed >= BLOCK_S, "the stub must actually block, else the test proves nothing"
    # Offloaded: ~elapsed/0.01 ticks. Run inline: the ticker is frozen, ~1 tick.
    assert len(ticks) > (elapsed / 0.02), (
        f"event loop was starved: only {len(ticks)} ticks in {elapsed:.2f}s"
    )
