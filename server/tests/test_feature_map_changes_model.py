"""feature_map_changes: the per-product pointer to the ccswitch-owned map-changes PR (roadmap spec §9)."""
from __future__ import annotations

import asyncio
import tempfile


def test_pointer_row_round_trips_and_defaults_open():
    from sqlalchemy import select
    from server.db import db_session, init_db
    from server.db.models import FEATURE_MAP_CHANGE_STATUSES, FeatureMapChange, Project

    assert FEATURE_MAP_CHANGE_STATUSES == ("open", "merged", "abandoned")

    async def _go(db_url: str) -> tuple[str | None, str | None, int | None]:
        await init_db(db_url)
        async with db_session() as db:
            p = Project(slug="kindash", name="KinDash")
            db.add(p)
            await db.flush()
            db.add(FeatureMapChange(project_id=p.id, repo="cairnea/KinDash",
                                    branch="pm/map-changes-kindash"))
            await db.commit()
        async with db_session() as db:
            row = (await db.execute(select(FeatureMapChange))).scalar_one()
            return row.status, row.pr_url, row.pr_number

    with tempfile.TemporaryDirectory() as tmp:
        status, pr_url, pr_number = asyncio.run(_go(f"sqlite+aiosqlite:///{tmp}/t.db"))
    assert (status, pr_url, pr_number) == ("open", None, None)
