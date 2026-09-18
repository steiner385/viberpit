"""One-shot: convert pre-ladder ``needs_human`` rows on the LIVE DB (spec 2026-09-18 §3).

Nothing runs ``alembic upgrade`` at deploy, so after the hold columns are live
(``init_db`` reconcile adds them) run this once per host that owns the DB:

    python scripts/convert_legacy_needs_human.py --dry-run
    python scripts/convert_legacy_needs_human.py --apply

Reads the DB URL from the app settings (the same env the server runs with); pass
``db_url`` to ``main()`` to override.

Runs exactly ``server.pm.holds.LEGACY_CONVERSION_SQL``; prints the affected count first.
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from server.config import get_settings
from server.pm.holds import LEGACY_CONVERSION_SQL


async def main(apply: bool, db_url: str | None = None) -> int:
    url = db_url or get_settings().db_url
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            n = await conn.scalar(text(
                "SELECT count(*) FROM work_items WHERE triage_state = 'needs_human'"))
            print(f"legacy needs_human rows: {n}")
            if not apply:
                print("dry run; pass --apply to convert")
                return 0
            for stmt in LEGACY_CONVERSION_SQL:
                await conn.execute(text(stmt))
            left = await conn.scalar(text(
                "SELECT count(*) FROM work_items WHERE triage_state = 'needs_human'"))
            print(f"converted; remaining legacy rows: {left}")
            return 0 if left == 0 else 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--apply" in sys.argv)))
