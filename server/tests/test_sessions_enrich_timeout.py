"""Enrichment timeout isolation for the deprecated per-session helper (issue #660).

``_enrich_one`` is no longer on the GET /api/sessions call path — the route batches
enrichment via ``_enrich_batch`` (#742) and falls back per-session only through
``_each``, not through this function. ``_enrich_one`` survives solely as the
test-exercised ancestor of that fallback logic (see its docstring in
server/api/sessions.py), so this file exercises it directly: each of its per-field
RPC calls (suggestions, prs, summary) must be bounded by ENRICH_FETCH_TIMEOUT, and a
single slow/hung RPC must not stall the whole gather for the 30s httpx default.

The route's own batching/concurrency behavior is covered separately by
server/tests/test_enrichment_batch_route.py — this file does NOT exercise the route.
"""
from __future__ import annotations

import asyncio
import time
import types


from server.api.sessions import _enrich_one, ENRICH_FETCH_TIMEOUT


def _make_record(session_id: str = "s1", cwd: str | None = None):
    return types.SimpleNamespace(
        id=session_id,
        name="Test",
        repo=None,
        cwd=cwd,
    )


class _OkOrch:
    async def latest_suggestions(self, sid):
        return ["suggestion"]

    async def prs_for(self, sid):
        return []

    async def latest_summary(self, sid, *, viewing=False):
        return "summary text"


class _HungOrch:
    """All enrichment RPCs hang indefinitely."""

    async def latest_suggestions(self, sid):
        await asyncio.sleep(1000)

    async def prs_for(self, sid):
        await asyncio.sleep(1000)

    async def latest_summary(self, sid, *, viewing=False):
        await asyncio.sleep(1000)


class _RaisingOrch:
    """All enrichment RPCs raise."""

    async def latest_suggestions(self, sid):
        raise RuntimeError("suggestions boom")

    async def prs_for(self, sid):
        raise RuntimeError("prs boom")

    async def latest_summary(self, sid, *, viewing=False):
        raise RuntimeError("summary boom")


# ---------------------------------------------------------------------------
# _enrich_one unit tests
# ---------------------------------------------------------------------------


def test_enrich_one_happy_path():
    """_enrich_one returns enrichment data from a healthy orch."""
    rec = _make_record()
    repo, sugg, prs, summary = asyncio.run(
        _enrich_one(_OkOrch(), rec, viewing=False, timeout=2.0)
    )
    assert sugg == ["suggestion"]
    assert prs == []
    assert summary == "summary text"


def test_enrich_one_degrades_on_timeout():
    """Hung RPCs degrade to safe empty values within the timeout."""
    rec = _make_record()
    start = time.monotonic()
    repo, sugg, prs, summary = asyncio.run(
        _enrich_one(_HungOrch(), rec, viewing=False, timeout=0.1)
    )
    elapsed = time.monotonic() - start

    # All degrade to empty defaults.
    assert sugg == []
    assert prs == []
    assert summary == ""
    # Bounded: must finish within ~timeout, not hang × 3.
    # Generous ceiling for loaded CI boxes; well below the 30s httpx default.
    assert elapsed < 0.5


def test_enrich_one_degrades_on_exception():
    """Raising RPCs degrade gracefully to empty values."""
    rec = _make_record()
    repo, sugg, prs, summary = asyncio.run(
        _enrich_one(_RaisingOrch(), rec, viewing=False, timeout=2.0)
    )
    assert sugg == []
    assert prs == []
    assert summary == ""


# ---------------------------------------------------------------------------
# Concurrency within a single _enrich_one call: N *sessions calling _enrich_one*
# must not serialize on each other. This is NOT the route's batching behavior —
# see server/tests/test_enrichment_batch_route.py for that.
# ---------------------------------------------------------------------------


def test_enrich_multiple_sessions_is_concurrent():
    """N sessions with hung enrichment must complete in ~1× timeout, not N×."""
    records = [_make_record(f"s{i}") for i in range(5)]
    orch = _HungOrch()

    async def go():
        start = time.monotonic()
        results = await asyncio.gather(
            *(_enrich_one(orch, r, viewing=False, timeout=0.1) for r in records)
        )
        return results, time.monotonic() - start

    results, elapsed = asyncio.run(go())

    assert len(results) == 5
    for repo, sugg, prs, summary in results:
        assert sugg == []
        assert prs == []
        assert summary == ""
    # Concurrent: total ≈ 1× timeout, not 5×.
    assert elapsed < 0.5


# ---------------------------------------------------------------------------
# ENRICH_FETCH_TIMEOUT default sanity check
# ---------------------------------------------------------------------------


def test_enrich_fetch_timeout_default_is_tight():
    """Default timeout must be ≤ 5s — tight enough to meet the p99 target."""
    assert ENRICH_FETCH_TIMEOUT <= 5.0
