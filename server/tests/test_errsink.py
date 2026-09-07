# server/tests/test_errsink.py
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest


def _rec(msg="boom", *, level=logging.ERROR, name="server.x", **extra):
    rec = logging.LogRecord(name, level, "/repo/server/x.py", 10, msg, (), None, func="f")
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def _sink():
    from server.obs.errsink import ErrorSink
    return ErrorSink(source="edge", host="dobby")


def test_emit_aggregates_same_fingerprint():
    sink = _sink()
    for i in range(10):
        sink.emit(_rec(f"db locked after {i} retries", session_id=f"s{i % 2}"))
    pend = sink.drain()
    assert len(pend) == 1
    p = next(iter(pend.values()))
    assert p.count == 10
    assert p.sessions == ["s0", "s1"]
    assert sink.drain() == {}  # drain empties


def test_emit_ignores_info_and_own_logger():
    sink = _sink()
    sink.emit(_rec("info", level=logging.INFO))
    sink.emit(_rec("x", name="server.obs.errsink"))
    assert sink.drain() == {}


def test_emit_is_bounded_and_counts_drops(monkeypatch):
    from server.obs import errsink, metrics
    monkeypatch.setattr(errsink, "MAX_PENDING", 2)
    before = metrics.error_sink_dropped_total.value()
    sink = _sink()
    for msg in ("alpha", "beta", "gamma"):
        sink.emit(_rec(msg))
    assert len(sink.drain()) == 2
    assert sink.dropped == 1
    assert metrics.error_sink_dropped_total.value() == before + 1


def test_client_error_counted_once_despite_bare_reemit():
    sink = _sink()
    sink.emit(_rec("client.error", name="client", event="client.error",
                   fields={"msg": "TypeError: x", "stack": ""}))
    sink.emit(_rec("TypeError: x", name="client", source="frontend"))
    pend = sink.drain()
    assert len(pend) == 1
    p = next(iter(pend.values()))
    assert p.count == 1 and p.sig.source == "client"


def test_emit_never_raises(monkeypatch):
    from server.obs import errsink
    def boom(*a, **k):
        raise RuntimeError("signature exploded")
    monkeypatch.setattr(errsink, "signature_of", boom)
    sink = _sink()
    sink.emit(_rec("x"))  # must not raise
    assert sink.drain() == {}


def test_emit_is_reentrancy_safe(monkeypatch):
    """A record logged while the sink is inside emit() is ignored, not recursed."""
    from server.obs import errsink
    sink = _sink()
    real = errsink.signature_of
    def nested(record, **kw):
        sink.emit(_rec("nested"))  # re-enter
        return real(record, **kw)
    monkeypatch.setattr(errsink, "signature_of", nested)
    sink.emit(_rec("outer"))
    pend = sink.drain()
    assert [p.sig.sample_message for p in pend.values()] == ["outer"]


@pytest.mark.asyncio
async def test_flush_inserts_then_increments(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import ErrorSignature
    sink = _sink()
    for _ in range(3):
        sink.emit(_rec("boom", session_id="s1"))
    assert await sink.flush_once() == 1
    for _ in range(2):
        sink.emit(_rec("boom", session_id="s2"))
    await sink.flush_once()
    async with db_session() as db:
        row = (await db.execute(select(ErrorSignature))).scalars().one()
    assert row.count == 5 and row.count_since_filed == 5
    assert row.status == "observed" and row.source == "edge"
    assert row.sample_context["recent_sessions"] == ["s2", "s1"]
    assert row.sample_context["host"] == "dobby"
    assert row.first_seen_at <= row.last_seen_at


@pytest.mark.asyncio
async def test_flush_refreshes_sample_only_when_stale(isolated_db):
    from sqlalchemy import select, update
    from server.db import db_session
    from server.db.models import ErrorSignature
    sink = _sink()
    sink.emit(_rec("db locked after 1 retries"))
    await sink.flush_once()
    sink.emit(_rec("db locked after 2 retries"))  # same fingerprint, newer text
    await sink.flush_once()
    async with db_session() as db:
        row = (await db.execute(select(ErrorSignature))).scalars().one()
        assert row.sample_message == "db locked after 1 retries"  # fresh → kept
        await db.execute(update(ErrorSignature).values(
            sample_at=row.sample_at - timedelta(hours=25)))
    sink.emit(_rec("db locked after 3 retries"))
    await sink.flush_once()
    async with db_session() as db:
        row = (await db.execute(select(ErrorSignature))).scalars().one()
    assert row.sample_message == "db locked after 3 retries"  # stale → refreshed


@pytest.mark.asyncio
async def test_flush_failure_requeues_bounded(monkeypatch):
    from server.obs import errsink
    async def fail(batch):
        raise RuntimeError("db down")
    monkeypatch.setattr(errsink, "_upsert", fail)
    sink = _sink()
    sink.emit(_rec("boom"))
    await sink.flush_once()
    pend = sink.drain()
    assert len(pend) == 1 and next(iter(pend.values())).count == 1


@pytest.mark.asyncio
async def test_flush_failure_merges_into_live_entry(monkeypatch):
    """A failed flush must fold its sessions into a live entry, not just count/first_seen."""
    from server.obs import errsink
    sink = _sink()
    sink.emit(_rec("boom", session_id="s1"))

    async def fail(batch):
        # A live entry for the same fingerprint is created WHILE the failed batch
        # (holding "s1") is in flight — the merge-back must fold "s1" into it.
        sink.emit(_rec("boom", session_id="s2"))
        raise RuntimeError("db down")

    monkeypatch.setattr(errsink, "_upsert", fail)
    await sink.flush_once()
    pend = sink.drain()
    assert len(pend) == 1
    p = next(iter(pend.values()))
    assert p.count == 2
    assert p.sessions == ["s2", "s1"]


@pytest.mark.asyncio
async def test_start_and_stop_drain(isolated_db, monkeypatch):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import ErrorSignature
    from server.obs import errsink
    monkeypatch.setattr(errsink, "FLUSH_INTERVAL_S", 0.05)
    sink = _sink()
    sink.start()
    assert sink.is_running
    sink.start()  # idempotent
    sink.emit(_rec("boom"))
    row = None
    for _ in range(40):
        await asyncio.sleep(0.05)
        async with db_session() as db:
            row = (await db.execute(select(ErrorSignature))).scalars().one_or_none()
        if row is not None and row.count == 1:
            break
    assert row is not None and row.count == 1, "background flush did not land within 2s"
    sink.emit(_rec("boom"))
    await sink.stop()  # cancels the loop, then flushes what is pending
    assert not sink.is_running
    async with db_session() as db:
        assert (await db.execute(select(ErrorSignature))).scalars().one().count == 2


def test_configure_logging_installs_sink_once(monkeypatch):
    from server.obs import errsink, jsonlog
    root = logging.getLogger()
    before = list(root.handlers)
    monkeypatch.setattr(jsonlog, "_configured", False)
    monkeypatch.delenv("CCSWITCH_ERROR_SINK_ENABLED", raising=False)
    errsink.reset_for_tests()
    try:
        jsonlog.configure_logging(process="daemon")
        sink = errsink.get_error_sink()
        assert sink is not None and sink in root.handlers
        assert sink._source == "daemon"
        assert errsink.install_error_sink(source="edge") is sink  # idempotent, keeps the first
    finally:
        errsink.reset_for_tests()
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)


def test_configure_logging_respects_kill_switch(monkeypatch):
    from server.obs import errsink, jsonlog
    root = logging.getLogger()
    before = list(root.handlers)
    monkeypatch.setattr(jsonlog, "_configured", False)
    monkeypatch.setenv("CCSWITCH_ERROR_SINK_ENABLED", "false")
    errsink.reset_for_tests()
    try:
        jsonlog.configure_logging()
        assert errsink.get_error_sink() is None
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)


def test_edge_lifespan_runs_the_flusher(monkeypatch, tmp_path):
    """The edge lifespan starts the sink's flusher after init_db and stops it on shutdown."""
    from fastapi.testclient import TestClient
    from server.obs import errsink, jsonlog
    monkeypatch.setenv("CCSWITCH_DEV", "1")
    monkeypatch.delenv("CCSWITCH_TAILNET_ALLOWLIST", raising=False)
    monkeypatch.setenv("CCSWITCH_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'edge.db'}")
    monkeypatch.setattr(jsonlog, "_configured", False)
    root = logging.getLogger()
    before = list(root.handlers)
    errsink.reset_for_tests()
    try:
        from server.app import app
        with TestClient(app):
            sink = errsink.get_error_sink()
            assert sink is not None and sink.is_running
        assert not sink.is_running
    finally:
        errsink.reset_for_tests()
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
