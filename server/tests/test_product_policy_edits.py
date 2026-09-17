"""Policy revision and concurrent-write contract on the real database dialects."""
import asyncio
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.automations.product_policy import PolicyEditConflict, patch_project_policy
from server.db.models import Base, Project
from server.time_iso import iso_utc


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("initial", [None, {"dispatch": "off"}])
async def test_concurrent_policy_edits_have_one_winner(backend, initial, tmp_path, request, monkeypatch):
    """Both writers read before either writes; disjoint keys must not be lost."""
    schema = "policy_" + uuid4().hex
    admin = None
    if backend == "postgresql":
        url = request.getfixturevalue("pg_url")
        admin = create_async_engine(url)
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        from server.db import _make_engine
        engine = _make_engine(f"sqlite+aiosqlite:///{tmp_path}/policy.db")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        # A future, fixed clock also proves same-tick revisions advance strictly.
        frozen = datetime(2099, 1, 1, 12, 0, 0, 123456)
        monkeypatch.setattr("server.automations.product_policy.naive_utc_now", lambda: frozen)
        async with factory.begin() as db:
            p = Project(id="p", slug="p", name="p", automation_policy=initial,
                        automation_policy_updated_at=frozen if initial else None)
            db.add(p)
        ready = asyncio.Barrier(2)
        async def edit(patch):
            async with factory() as db:
                p = await db.get(Project, "p")
                await ready.wait()
                try:
                    await patch_project_policy(db, p, patch)
                    await db.commit()
                    return patch
                except PolicyEditConflict:
                    await db.rollback()
                    return None
        results = await asyncio.gather(edit({"weight": 3}), edit({"max_in_flight": 2}))
        winners = [r for r in results if r is not None]
        assert len(winners) == 1, results
        async with factory.begin() as db:
            p = (await db.scalars(select(Project))).one()
            assert p.automation_policy == {**(initial or {}), **winners[0]}
            first_revision = iso_utc(p.automation_policy_updated_at)
            # Real refresh and exact UTC revision survive serialization on both dialects.
            await patch_project_policy(db, p, {"dispatch": "off"},
                                       check_expected=True, expected_updated_at=datetime.fromisoformat(first_revision))
            assert iso_utc(p.automation_policy_updated_at) > first_revision
            before_reset = p.automation_policy_updated_at
            await patch_project_policy(db, p, None)
            assert p.automation_policy is None
            assert iso_utc(p.automation_policy_updated_at) > iso_utc(before_reset)
            with pytest.raises(PolicyEditConflict):
                await patch_project_policy(db, p, {"dispatch": "auto"},
                                           check_expected=True, expected_updated_at=before_reset)
            with pytest.raises(PolicyEditConflict):
                await patch_project_policy(db, p, {"dispatch": "auto"},
                                           check_expected=True, expected_updated_at=None)
            assert p.automation_policy is None
    finally:
        await engine.dispose()
        if admin:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()
