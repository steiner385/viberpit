"""Protocol-version gating in probe_host (Ruling C).

A daemon whose /info reports a protocol version incompatible with the edge's
PROTOCOL_VERSION must be ranked as dispatchable=False, not dispatched to and
then failed at create time. probe_host enforces this at probe time.
"""
from __future__ import annotations

import asyncio


def _seed(tmp_path, dbname, *hosts):
    from server.db import db_session, init_db

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / dbname}")
        async with db_session() as db:
            for h in hosts:
                db.add(h)
    return go


def test_mismatched_protocol_marks_unhealthy(tmp_path):
    """A daemon reporting a different protocol version must be marked unhealthy."""
    from sqlalchemy import select as sa_select

    from server.db import db_session
    from server.db.models import Host
    from server.ipc.protocol import PROTOCOL_VERSION
    from server.sessions.host_probe import probe_host

    mismatched_proto = PROTOCOL_VERSION + 1

    async def fake_fetch(endpoint):
        return {"sha": "deadbeef", "protocol": mismatched_proto, "loadAvg": 0.5}

    async def run():
        await _seed(tmp_path, "proto_mismatch.db",
                    Host(name="alien", endpoint="https://alien:9443"))()
        result = await probe_host("alien", "https://alien:9443", fetch=fake_fetch)
        async with db_session() as db:
            row = (await db.execute(
                sa_select(Host).where(Host.name == "alien"))).scalars().first()
        return result, row

    result, row = asyncio.run(run())
    assert row.healthy is False, "protocol mismatch must mark host unhealthy"
    assert row.last_error is not None
    assert "protocol mismatch" in row.last_error
    assert str(mismatched_proto) in row.last_error
    assert str(PROTOCOL_VERSION) in row.last_error
    # Metrics are still recorded — the row is useful for audit even if unhealthy
    assert row.sha == "deadbeef"
    assert row.protocol == mismatched_proto


def test_matching_protocol_marks_healthy(tmp_path):
    """A daemon reporting the same protocol version as the edge is healthy."""
    from sqlalchemy import select as sa_select

    from server.db import db_session
    from server.db.models import Host
    from server.ipc.protocol import PROTOCOL_VERSION
    from server.sessions.host_probe import probe_host

    async def fake_fetch(endpoint):
        return {"sha": "abc1234", "protocol": PROTOCOL_VERSION,
                "loadAvg": 1.0, "memAvailableMb": 4096, "activeSessions": 2}

    async def run():
        await _seed(tmp_path, "proto_ok.db",
                    Host(name="rocky", endpoint="https://rocky:9443"))()
        result = await probe_host("rocky", "https://rocky:9443", fetch=fake_fetch)
        async with db_session() as db:
            row = (await db.execute(
                sa_select(Host).where(Host.name == "rocky"))).scalars().first()
        return result, row

    result, row = asyncio.run(run())
    assert row.healthy is True, "matching protocol version must be healthy"
    assert row.last_error is None
    assert row.sha == "abc1234"
    assert row.protocol == PROTOCOL_VERSION


def test_missing_protocol_field_is_not_gated(tmp_path):
    """If the daemon doesn't report a protocol field (older daemon), don't gate it.

    This preserves backward compatibility: a None protocol means unknown, not
    mismatched — we only gate when the daemon explicitly reports an incompatible
    version number.
    """
    from sqlalchemy import select as sa_select

    from server.db import db_session
    from server.db.models import Host
    from server.sessions.host_probe import probe_host

    async def fake_fetch(endpoint):
        return {"sha": "oldsha"}  # no 'protocol' key at all

    async def run():
        await _seed(tmp_path, "proto_none.db",
                    Host(name="old-daemon", endpoint="https://old:9443"))()
        result = await probe_host("old-daemon", "https://old:9443", fetch=fake_fetch)
        async with db_session() as db:
            row = (await db.execute(
                sa_select(Host).where(Host.name == "old-daemon"))).scalars().first()
        return result, row

    result, row = asyncio.run(run())
    assert row.healthy is True, "missing protocol field must not gate the host"
    assert row.last_error is None
