"""Unit tests for fenced host heartbeat ops (spec 002 FR-101/FR-201, Task 2)."""
import pytest
from datetime import datetime, timedelta
from server.sessions import host_heartbeat as hb
from server.db import db_session
from server.db.models import HostHeartbeat
from sqlalchemy import update


async def _age(host: str, seconds: float) -> None:
    async with db_session() as db:
        await db.execute(update(HostHeartbeat).where(HostHeartbeat.host == host)
                         .values(beat_at=datetime.utcnow() - timedelta(seconds=seconds)))


@pytest.mark.asyncio
async def test_register_is_idempotent_on_fence(isolated_db):
    assert await hb.register("w1") == 1
    assert await hb.register("w1") == 1


@pytest.mark.asyncio
async def test_beat_fails_after_fence_bump(isolated_db):
    fence = await hb.register("w1")
    await _age("w1", 999)
    assert await hb.declare_dead("w1", dead_after_s=300, observed_fence=fence) == fence + 1
    assert await hb.beat("w1", fence) is False          # old holder is fenced out
    assert await hb.beat("w1", fence + 1) is True       # re-registered holder beats fine


@pytest.mark.asyncio
async def test_declare_dead_single_winner(isolated_db):
    fence = await hb.register("w1")
    await _age("w1", 999)
    assert await hb.declare_dead("w1", dead_after_s=300, observed_fence=fence) == fence + 1
    assert await hb.declare_dead("w1", dead_after_s=300, observed_fence=fence) is None


@pytest.mark.asyncio
async def test_declare_dead_refuses_fresh_row(isolated_db):
    fence = await hb.register("w1")
    assert await hb.declare_dead("w1", dead_after_s=300, observed_fence=fence) is None
