"""Registry registration + idempotent startup upsert."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select


class _Fake:
    id = "fake"
    name = "Fake"
    description = "d"
    modes = ("off", "on")
    config_schema: list = []

    def status(self, ctx):
        from server.automations.base import AutomationStatus
        return AutomationStatus(text="idle")

    async def on_state_change(self, **kw): ...


def test_register_and_get():
    from server.automations.registry import AutomationRegistry
    r = AutomationRegistry()
    r.register(_Fake())
    assert r.get("fake").name == "Fake"
    assert [a.id for a in r.all()] == ["fake"]


def test_duplicate_id_rejected():
    from server.automations.registry import AutomationRegistry
    r = AutomationRegistry()
    r.register(_Fake())
    with pytest.raises(ValueError, match="already registered"):
        r.register(_Fake())


def test_gh_reconcile_registered():
    """The reconciler must be in the shared registry (003 §6) with its three modes,
    or ensure_rows() never creates its row and the automations UI cannot enable it."""
    from server.automations.registry import registry
    a = registry.get("gh-reconcile")
    assert a.modes == ("off", "dry_run", "apply")


def test_ensure_rows_upserts_then_preserves_state(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'x.db'}"

    async def run():
        from server.db import init_db, db_session
        from server.automations.registry import AutomationRegistry
        from server.db.models import Automation

        await init_db(db_url=db_url)

        r = AutomationRegistry()
        r.register(_Fake())

        # First ensure_rows: creates absent row with enabled=False
        await r.ensure_rows()
        async with db_session() as db:
            row = (await db.execute(
                select(Automation).where(Automation.id == "fake")
            )).scalar_one()
            assert row.enabled is False  # default

            # Simulate a user toggle
            row.enabled = True

        # Second ensure_rows: must NOT reset enabled back to False
        await r.ensure_rows()
        async with db_session() as db:
            row = (await db.execute(
                select(Automation).where(Automation.id == "fake")
            )).scalar_one()
            assert row.enabled is True

    asyncio.run(run())
