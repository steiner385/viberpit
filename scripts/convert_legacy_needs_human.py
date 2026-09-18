"""One-shot: convert pre-ladder ``needs_human`` rows on the LIVE DB (spec 2026-09-18 §3).

Nothing runs ``alembic upgrade`` at deploy, so after the hold columns are live
(``init_db`` reconcile adds them) run this once per host that owns the DB:

    CCSWITCH_DB_URL=… python scripts/convert_legacy_needs_human.py --dry-run
    CCSWITCH_DB_URL=… python scripts/convert_legacy_needs_human.py --apply

Runs exactly ``server.pm.holds.LEGACY_CONVERSION_SQL``; prints the affected count first.
"""
from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from server.pm.holds import LEGACY_CONVERSION_SQL


async def main(apply: bool) -> int:
    url = os.environ.get("CCSWITCH_DB_URL") or os.environ.get("VIBERPIT_DB_URL")
    if not url:
        print("set CCSWITCH_DB_URL", file=sys.stderr)
        return 2
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
