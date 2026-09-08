"""A driver rebuilt from the in-memory record must keep the session's permission mode.

Reproduces the 2026-09-07/08 "Permission needed" cards on full-auto Claude sessions:
the spawn path seeded its in-memory ``SessionRecord`` without ``permission_mode``, so
the record silently carried the dataclass default (``interactive``) while the DB row
said ``full-auto``. Every driver rebuilt from that record without a daemon restart in
between — after ``mark_complete`` released the process, after an idle reap, after a
detach — ran in ``interactive`` and raised a card for the first Bash call the CLI did
not approve on its own. A daemon restart rebuilt the records from their rows and made
the bug vanish, which is why it looked intermittent.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from server.sessions.permissions import PermissionRegistry
from server.tests.test_session_queue_drain import _make_orch, _stop_orch


def test_spawned_record_carries_the_requested_permission_mode(tmp_path):
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'p.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=3)
        try:
            rec = await orch.create_session("probe", permission_mode="full-auto")
            return orch.get_session(rec.id).permission_mode, orch.driver(rec.id).mode
        finally:
            await _stop_orch(orch)

    record_mode, driver_mode = asyncio.run(run())
    assert driver_mode == "full-auto"
    assert record_mode == "full-auto", (
        "the in-memory record is what every later driver rebuild reads its mode from")


def test_driver_rebuilt_after_complete_keeps_the_spawn_permission_mode(tmp_path):
    """``mark_complete`` releases the process; the next prompt rebuilds the driver from
    the in-memory record. That driver must run in the mode the session was created
    with, not the record's default."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'p.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=3)
        try:
            rec = await orch.create_session("probe", permission_mode="full-auto")
            assert orch.driver(rec.id).mode == "full-auto"
            # Exactly what a mark_complete signal does: persist `complete`, which pops
            # and releases the live driver while the record stays registered.
            await orch._persist_status(rec.id, "complete")
            release = orch._releasing.get(rec.id)
            if release is not None:
                await release
            assert orch.driver(rec.id) is None, "complete must release the driver"
            rebuilt = await orch.ensure_driver(rec.id)
            assert rebuilt is not None
            return rebuilt.mode
        finally:
            await _stop_orch(orch)

    assert asyncio.run(run()) == "full-auto"


def test_registry_logs_every_card_it_raises(caplog):
    """The one line that would have named this bug on day one: a card raised in a mode
    the operator believes never prompts."""
    async def run():
        reg = PermissionRegistry()
        with caplog.at_level(logging.INFO, logger="server.sessions.permissions"):
            perm_id, fut, _ = await reg.request(
                "sess-1", "turn-1", "Bash", {"command": "sed -i s/a/b/ x.md"},
                mode="interactive", denylist=PermissionRegistry.DANGEROUS,
                allowlist=True, identity="tony")
        assert perm_id is not None
        reg.deny_all("sess-1")
        await fut
        return [r for r in caplog.records if r.name == "server.sessions.permissions"]

    records = asyncio.run(run())
    assert records, "raising a card must log it"
    msg = records[-1].getMessage()
    assert "session=sess-1" in msg and "tool=Bash" in msg and "mode=interactive" in msg, msg


@pytest.mark.parametrize("mode", ["full-auto", "bypassPermissions"])
def test_registry_stays_silent_when_it_auto_approves(caplog, mode):
    async def run():
        reg = PermissionRegistry()
        with caplog.at_level(logging.INFO, logger="server.sessions.permissions"):
            perm_id, fut, _ = await reg.request(
                "sess-1", "turn-1", "Bash", {"command": "echo hi"},
                mode=mode, denylist=PermissionRegistry.DANGEROUS,
                allowlist=True, identity="tony")
        assert perm_id is None and await fut is True
        return [r for r in caplog.records if r.name == "server.sessions.permissions"]

    assert asyncio.run(run()) == []
