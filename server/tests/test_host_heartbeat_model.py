"""Unit test — HostHeartbeat model roundtrip (spec 002 FR-101, Task 1)."""
import pytest
from server.db import db_session
from server.db.models import HostHeartbeat


@pytest.mark.asyncio
async def test_host_heartbeat_roundtrip(isolated_db):
    async with db_session() as db:
        db.add(HostHeartbeat(host="rocky", epoch="1a2b-42"))
    async with db_session() as db:
        row = await db.get(HostHeartbeat, "rocky")
        assert row is not None and row.fence == 1 and row.beat_at is not None
