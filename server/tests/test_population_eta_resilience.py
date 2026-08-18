"""ETA fetch resilience (issue #671): stale-while-revalidate, jittered retry,
and the shared API-budget priority gate.

Reuses the seeded-history helpers from test_population_eta_wired to build a
poller whose ETA compute yields a real value on success.
"""
from __future__ import annotations

import asyncio

from server.github.api_budget import PriorityGate
from server.github.pr_poller import PrStatusPoller
from server.github.pr_population import PrPopulationPoller

from .test_population_eta_wired import (
    NOW,
    REPO,
    _build_db,
    _eta_checks_resp_empty,
    _pop_resp,
)


async def _noop_sleep(_secs: float) -> None:
    return None


# ---------------------------------------------------------------------------
# Stale-while-revalidate
# ---------------------------------------------------------------------------

def test_stale_while_revalidate_reserves_last_good(tmp_path):
    """A first successful cycle caches the ETA; when the next fetch fails after
    retry, the PR keeps its last-good eta_seconds marked eta_stale=True."""
    store = _build_db(tmp_path / "ph.db")
    fail = {"on": False}

    def eta_qfn(prs):
        if fail["on"]:
            raise RuntimeError("rate limited")
        return _eta_checks_resp_empty(prs)

    p = PrPopulationPoller(
        query_fn=lambda repo: _pop_resp(42),
        eta_query_fn=eta_qfn,
        history_store=store,
        now_fn=lambda: NOW,
        sleep_fn=_noop_sleep,
    )

    # Cycle 1 — success populates the last-good cache.
    asyncio.run(p.refresh_once([REPO]))
    fresh = p.cached(REPO)[0]
    assert fresh.eta_seconds is not None
    assert fresh.eta_stale is False
    assert fresh.eta_updated_at == NOW.isoformat()
    good_eta = fresh.eta_seconds

    # Cycle 2 — fetch (and its retry) fail; last-good ETA re-served as stale.
    fail["on"] = True
    asyncio.run(p.refresh_once([REPO]))
    stale = p.cached(REPO)[0]
    assert stale.eta_seconds == good_eta
    assert stale.eta_stale is True
    assert stale.eta_updated_at == NOW.isoformat()


def test_no_cache_failure_leaves_defaults(tmp_path):
    """A first-ever fetch failure (no cached value) leaves ETA at defaults —
    nothing to re-serve, but no crash and no bogus stale flag."""
    store = _build_db(tmp_path / "ph.db")

    def eta_qfn(prs):
        raise RuntimeError("rate limited")

    p = PrPopulationPoller(
        query_fn=lambda repo: _pop_resp(7),
        eta_query_fn=eta_qfn,
        history_store=store,
        now_fn=lambda: NOW,
        sleep_fn=_noop_sleep,
    )
    asyncio.run(p.refresh_once([REPO]))
    pr = p.cached(REPO)[0]
    assert pr.eta_seconds is None
    assert pr.eta_stale is False


def test_cache_pruned_when_pr_closes(tmp_path):
    """A PR that leaves the open population drops out of the stale cache, so a
    later failure can't resurrect a dead PR's ETA."""
    store = _build_db(tmp_path / "ph.db")
    state = {"num": 42, "eta_state": "OPEN"}

    def eta_qfn(prs):
        return _eta_checks_resp_empty(prs)

    p = PrPopulationPoller(
        query_fn=lambda repo: _pop_resp(state["num"], state=state["eta_state"]),
        eta_query_fn=eta_qfn,
        history_store=store,
        now_fn=lambda: NOW,
        sleep_fn=_noop_sleep,
    )
    asyncio.run(p.refresh_once([REPO]))
    assert (REPO, 42) in p._eta_cache

    # PR merges → no longer open → pruned from the cache.
    state["eta_state"] = "MERGED"
    asyncio.run(p.refresh_once([REPO]))
    assert (REPO, 42) not in p._eta_cache


# ---------------------------------------------------------------------------
# Jittered retry
# ---------------------------------------------------------------------------

def test_retry_succeeds_second_attempt(tmp_path):
    """A transient first-attempt failure is cleared by the single retry; ETA is
    computed and the retry sleep used the injected jitter range."""
    store = _build_db(tmp_path / "ph.db")
    calls = {"n": 0}
    slept = []

    def eta_qfn(prs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient blip")
        return _eta_checks_resp_empty(prs)

    async def _record_sleep(secs):
        slept.append(secs)

    p = PrPopulationPoller(
        query_fn=lambda repo: _pop_resp(42),
        eta_query_fn=eta_qfn,
        history_store=store,
        now_fn=lambda: NOW,
        sleep_fn=_record_sleep,
        rng=lambda: 0.5,  # deterministic midpoint of [5, 30] -> 17.5s
        eta_retry_min_s=5.0,
        eta_retry_max_s=30.0,
    )
    asyncio.run(p.refresh_once([REPO]))

    assert calls["n"] == 2
    assert slept == [17.5]
    pr = p.cached(REPO)[0]
    assert pr.eta_seconds is not None
    assert pr.eta_stale is False


# ---------------------------------------------------------------------------
# Priority gate
# ---------------------------------------------------------------------------

def test_gate_idle_by_default():
    async def _run():
        gate = PriorityGate()
        assert gate.busy is False
        assert await gate.wait_until_idle(timeout=0.01) is True
    asyncio.run(_run())


def test_gate_busy_during_high_priority():
    async def _run():
        gate = PriorityGate()
        async with gate.high_priority():
            assert gate.busy is True
            # A low-priority waiter times out while high work is in flight.
            assert await gate.wait_until_idle(timeout=0.01) is False
        assert gate.busy is False
        assert await gate.wait_until_idle(timeout=0.01) is True
    asyncio.run(_run())


def test_gate_nested_high_priority_refcounts():
    async def _run():
        gate = PriorityGate()
        async with gate.high_priority():
            async with gate.high_priority():
                assert gate.busy is True
            # Outer section still active -> still busy after inner exits.
            assert gate.busy is True
        assert gate.busy is False
    asyncio.run(_run())


def test_eta_fetch_waits_for_gate_then_proceeds(tmp_path):
    """When a gate is set, the ETA fetch calls wait_until_idle before spending
    budget, then proceeds and computes an ETA."""
    store = _build_db(tmp_path / "ph.db")
    gate = PriorityGate()
    waited = []

    class _SpyGate(PriorityGate):
        async def wait_until_idle(self, timeout=None):
            waited.append(timeout)
            return await super().wait_until_idle(timeout)

    spy = _SpyGate()

    p = PrPopulationPoller(
        query_fn=lambda repo: _pop_resp(42),
        eta_query_fn=_eta_checks_resp_empty,
        history_store=store,
        now_fn=lambda: NOW,
        sleep_fn=_noop_sleep,
        api_gate=spy,
        eta_gate_timeout=12.0,
    )
    asyncio.run(p.refresh_once([REPO]))
    assert waited == [12.0]
    assert p.cached(REPO)[0].eta_seconds is not None
    del gate  # silence unused


def test_pr_poller_marks_gate_busy_during_query():
    """PrStatusPoller wraps its network call in the gate's high_priority section,
    so a concurrent ETA waiter yields for the query's duration (issue #671)."""
    gate = PriorityGate()
    seen_busy = []

    def query_fn(chunk):
        # The gh call runs in a worker thread; the gate was marked busy on the
        # event loop before to_thread, so it must be busy while we're in here.
        seen_busy.append(gate.busy)
        return {"data": {}}

    poller = PrStatusPoller(query_fn=query_fn, api_gate=gate)
    asyncio.run(poller.refresh_once([("o/n", "main")]))

    assert seen_busy == [True]
    # Section exited -> gate idle again.
    assert gate.busy is False
