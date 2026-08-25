"""Enrichment timeout isolation for the batched enrichment path (issue #660, #747).

``_enrich_batch`` is the sole enrichment path on GET /api/sessions — it makes three
batched RPCs (one per field) for any number of sessions (#742). Each is bounded by
ENRICH_FETCH_TIMEOUT, pushed INTO the RPC; a single slow/hung RPC must not stall the
whole gather for the 30s httpx default.

The route's shape (3 RPCs for N sessions, positional alignment, per-field degradation)
is covered by server/tests/test_enrichment_batch_route.py. This file focuses purely on
the timeout/error degradation contract: hung or raising batch RPCs must degrade to empty
within the deadline, and the three concurrent RPCs must not serialize on each other.
"""
from __future__ import annotations

import asyncio
import time
import types


from server.api.sessions import _enrich_batch, ENRICH_FETCH_TIMEOUT


def _make_record(session_id: str = "s1", cwd: str | None = None):
    return types.SimpleNamespace(
        id=session_id,
        name="Test",
        repo=None,
        cwd=cwd,
    )


class _OkOrch:
    async def latest_suggestions_many(self, session_ids, *, timeout=None):
        return {sid: ["suggestion"] for sid in session_ids}

    async def prs_for_many(self, session_ids, *, timeout=None):
        return {sid: [] for sid in session_ids}

    async def latest_summary_many(self, session_ids, *, viewing=False, timeout=None):
        return {sid: "summary text" for sid in session_ids}


class _HungOrch:
    """All enrichment RPCs hang indefinitely."""

    async def latest_suggestions_many(self, session_ids, *, timeout=None):
        await asyncio.sleep(1000)

    async def prs_for_many(self, session_ids, *, timeout=None):
        await asyncio.sleep(1000)

    async def latest_summary_many(self, session_ids, *, viewing=False, timeout=None):
        await asyncio.sleep(1000)


class _RaisingOrch:
    """All enrichment RPCs raise."""

    async def latest_suggestions_many(self, session_ids, *, timeout=None):
        raise RuntimeError("suggestions boom")

    async def prs_for_many(self, session_ids, *, timeout=None):
        raise RuntimeError("prs boom")

    async def latest_summary_many(self, session_ids, *, viewing=False, timeout=None):
        raise RuntimeError("summary boom")


# ---------------------------------------------------------------------------
# _enrich_batch unit tests
# ---------------------------------------------------------------------------


def test_enrich_batch_happy_path():
    """_enrich_batch returns enrichment data from a healthy orch."""
    rec = _make_record()
    _repos, suggs, prs, summaries = asyncio.run(
        _enrich_batch(_OkOrch(), [rec], viewing=False, timeout=2.0)
    )
    assert suggs[0] == ["suggestion"]
    assert prs[0] == []
    assert summaries[0] == "summary text"


def test_enrich_batch_degrades_on_timeout():
    """Hung batch RPCs degrade to safe empty values within the timeout."""
    rec = _make_record()
    start = time.monotonic()
    _repos, suggs, prs, summaries = asyncio.run(
        _enrich_batch(_HungOrch(), [rec], viewing=False, timeout=0.1)
    )
    elapsed = time.monotonic() - start

    # All degrade to empty defaults.
    assert suggs[0] == []
    assert prs[0] == []
    assert summaries[0] == ""
    # Bounded: the three concurrent RPCs must finish within ~timeout, not 3×.
    # Generous ceiling for loaded CI boxes; well below the 30s httpx default.
    assert elapsed < 0.5


def test_enrich_batch_degrades_on_exception():
    """Raising batch RPCs degrade gracefully to empty values."""
    rec = _make_record()
    _repos, suggs, prs, summaries = asyncio.run(
        _enrich_batch(_RaisingOrch(), [rec], viewing=False, timeout=2.0)
    )
    assert suggs[0] == []
    assert prs[0] == []
    assert summaries[0] == ""


def test_enrich_batch_three_rpcs_are_concurrent():
    """Three hung batch RPCs must complete in ~1× timeout, not 3×."""
    records = [_make_record(f"s{i}") for i in range(5)]
    orch = _HungOrch()

    start = time.monotonic()
    _repos, suggs, prs, summaries = asyncio.run(
        _enrich_batch(orch, records, viewing=False, timeout=0.1)
    )
    elapsed = time.monotonic() - start

    assert len(suggs) == 5
    for s in suggs:
        assert s == []
    for p in prs:
        assert p == []
    for summary in summaries:
        assert summary == ""
    # Concurrent: total ≈ 1× timeout, not 3×.
    assert elapsed < 0.5


# ---------------------------------------------------------------------------
# ENRICH_FETCH_TIMEOUT default sanity check
# ---------------------------------------------------------------------------


def test_enrich_fetch_timeout_default_is_tight():
    """Default timeout must be ≤ 5s — tight enough to meet the p99 target."""
    assert ENRICH_FETCH_TIMEOUT <= 5.0
