"""The effective global cap = min(physical ceiling, attention ceiling) (spec 2026-09-20 D3, D11)."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server.sessions.global_cap import GlobalCap, drain_if_raised, read_global_cap, resolve_global_cap


def test_physical_is_the_sum_of_enabled_host_caps():
    gc = resolve_global_cap(host_caps=[("dobby", 4), ("rocky", 6)], attention=None)
    assert (gc.physical, gc.attention, gc.effective, gc.binding) == (10, None, 10, "physical")
    assert gc.hosts == (("dobby", 4), ("rocky", 6))


def test_any_uncapped_host_makes_the_physical_ceiling_unbounded():
    gc = resolve_global_cap(host_caps=[("dobby", 4), ("rocky", None)], attention=None)
    assert gc.physical is None
    assert (gc.effective, gc.binding) == (5, "default")


def test_no_hosts_and_no_attention_falls_back_to_the_default_floor():
    gc = resolve_global_cap(host_caps=[], attention=None)
    assert (gc.physical, gc.attention, gc.effective, gc.binding) == (None, None, 5, "default")


def test_attention_below_physical_binds():
    gc = resolve_global_cap(host_caps=[("dobby", 4), ("rocky", 6)], attention=6)
    assert (gc.effective, gc.binding) == (6, "attention")


def test_attention_equal_to_physical_binds_as_attention():
    """The typed value is the one the person will look for, so a tie names it."""
    gc = resolve_global_cap(host_caps=[("dobby", 4), ("rocky", 6)], attention=10)
    assert (gc.effective, gc.binding) == (10, "attention")


def test_attention_above_physical_is_ignored_and_physical_binds():
    gc = resolve_global_cap(host_caps=[("dobby", 4), ("rocky", 6)], attention=12)
    assert (gc.physical, gc.attention, gc.effective, gc.binding) == (10, 12, 10, "physical")


def test_attention_alone_binds_when_hosts_are_unbounded():
    gc = resolve_global_cap(host_caps=[("dobby", None)], attention=3)
    assert (gc.effective, gc.binding) == (3, "attention")


def test_effective_is_clamped_to_at_least_one():
    assert resolve_global_cap(host_caps=[], attention=0).effective == 1
    assert resolve_global_cap(host_caps=[], attention=None, default=0).effective == 1


def test_to_wire_shape():
    gc = resolve_global_cap(host_caps=[("dobby", 4)], attention=None)
    assert gc.to_wire() == {"physical": 4, "attention": None, "effective": 4,
                            "binding": "physical", "hosts": [{"name": "dobby", "cap": 4}]}


def test_drain_if_raised_fires_only_when_the_effective_cap_rose():
    orch = AsyncMock()
    lower = GlobalCap(10, 4, 4, "attention", ())
    higher = GlobalCap(10, 8, 8, "attention", ())

    async def run():
        assert drain_if_raised(orch, lower, higher) is True
        await asyncio.sleep(0)  # let the created task run
        orch.maybe_start_queued.assert_awaited_once()
        orch.maybe_start_queued.reset_mock()
        assert drain_if_raised(orch, higher, lower) is False
        assert drain_if_raised(orch, lower, lower) is False
        await asyncio.sleep(0)
        orch.maybe_start_queued.assert_not_awaited()
        # No orchestrator (minimal test apps): a no-op, never a crash.
        assert drain_if_raised(None, lower, higher) is False

    asyncio.run(run())


@pytest.mark.usefixtures("isolated_db")
def test_read_global_cap_reads_enabled_hosts_and_the_settings_row():
    from server.db import db_session
    from server.db.models import AppSettings, Host

    async def run():
        async with db_session() as db:
            db.add(Host(name="dobby", endpoint="https://dobby:9443", enabled=True, max_sessions=4))
            db.add(Host(name="rocky", endpoint="https://rocky:9443", enabled=True, max_sessions=6))
            # Disabled: contributes nothing, even though uncapped.
            db.add(Host(name="old", endpoint="https://old:9443", enabled=False, max_sessions=None))
            await db.flush()
        async with db_session() as db:
            gc = await read_global_cap(db)
            assert (gc.physical, gc.attention, gc.effective, gc.binding) == (10, None, 10, "physical")
        async with db_session() as db:
            db.add(AppSettings(id="singleton", max_active_sessions=3))
            await db.flush()
        async with db_session() as db:
            gc = await read_global_cap(db)
            assert (gc.effective, gc.binding) == (3, "attention")

    asyncio.run(run())


def test_no_other_reader_of_max_active_sessions():
    """D11: exactly one reader. `server/api/settings.py` may WRITE and ECHO the
    column; nobody but global_cap.py may apply the default or read it as a cap."""
    root = Path(__file__).resolve().parents[2]
    allowed = {"server/db/models.py", "server/sessions/global_cap.py"}
    offenders: list[str] = []
    for path in (root / "server").rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(("server/tests/", "server/db/migrations/")) or rel in allowed:
            continue
        text = path.read_text()
        if "DEFAULT_MAX_ACTIVE_SESSIONS" in text:
            offenders.append(f"{rel}: DEFAULT_MAX_ACTIVE_SESSIONS")
        if rel != "server/api/settings.py" and re.search(r"(?<![\w])max_active_sessions", text):
            offenders.append(f"{rel}: max_active_sessions")
    assert offenders == []
