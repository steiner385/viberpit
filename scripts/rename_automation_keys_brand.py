#!/usr/bin/env python3
"""ONE-SHOT: converge brand-prefixed keys in database rows off the pre-rebrand slug.

Spec: docs/superpowers/specs/2026-09-06-viberpit-rebrand-design.md §3, Phase 2c (#1369)
for the automation dedupe keys; epic #1365 for the SDLC control-band id.

    python scripts/rename_automation_keys_brand.py           # dry run: print the plan
    python scripts/rename_automation_keys_brand.py --apply   # perform it

An automation's dedupe key is ``<product-slug>:<kind>``, composed from the product's
CURRENT slug and then PERSISTED — so a product renamed off its pre-rebrand slug leaves
stored keys that nothing looks up any more. The symptom is not an error: the
find-or-create in ``AutomationContext.spawn_session`` simply finds nothing, spawns a
second session for work already running, and the loop guard keyed on the old string
does not stop it. There is no dual-accept for this (see ``server/db/brand_keys``);
converging the rows in place is the fix.

The control-band id (``<product-slug>-pr-cycle-time``, ``server/sdlc/bands.yaml``) is
persisted the same way, into the triage incident it files: as ``triage_items.metric``,
as the row's unique ``key`` and in its ``summary``. Renamed in the yaml alone, the
monitor's next tick would file the breach a second time as a fresh open incident beside
the one Intake already adopted. Same fix, same script.

Every statement comes from ``server/db/brand_keys``, which the alembic revisions
``0061_brand_automation_keys`` and ``0074_brand_control_band_id`` also drive — the
reviewable record and the thing that runs cannot describe different SQL. Nothing runs
``alembic upgrade`` at deploy time and reconcile applies no data changes, which is why
this script exists at all.

Idempotent: each statement selects on the legacy prefix and writes the new one, so a
second run matches zero rows. Safe to re-run after a partial failure.

The database is the one ``VIBERPIT_DB_URL`` names, and the run refuses unless the app
settings resolved that same URL — see :func:`db_url_error` for the two ways that goes
wrong, both of which end with rows written somewhere nobody intended.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server.db.brand_keys import (  # noqa: E402 -- repo root added above
    ALL_KEY_COLUMNS,
    KeyColumn,
    delete_collisions_sql,
    select_legacy_sql,
    update_sql,
)

#: Same mirroring as the other one-shots, for the explicit-database guard below.
DB_URL_ENV = "VIBERPIT_DB_URL"


def db_url_error(wanted: str | None, resolved: str) -> str | None:
    """Why this run must not proceed against *resolved*, or None if it may.

    Two distinct traps, which is why this is a function with its own tests:

    * Nothing set — the settings default would silently create an empty local SQLite
      file, in which there are no rows at all, and the run would report a clean
      all-clear against a database that is not the one anyone meant.
    * Set, but NOT what the settings resolved. ``env_compat.mirror_legacy_env()``
      fills each brand prefix from the other and does not overwrite, so on a host
      whose ``/etc`` env file already exports the legacy-prefixed name, that value
      WINS and this script would write to it while the operator believes they
      pointed it somewhere else. (Observed: a dry run naming a scratch SQLite file
      read the live cluster instead.)
    """
    if not wanted:
        return f"{DB_URL_ENV} must be set (the live database)"
    if wanted != resolved:
        return (f"{DB_URL_ENV} names a different database than the settings resolved.\n"
                f"  {DB_URL_ENV}: {wanted.rsplit('@', 1)[-1]}\n"
                f"  resolved:     {resolved.rsplit('@', 1)[-1]}\n"
                f"A legacy-prefixed DB URL already in the environment takes precedence. "
                f"Unset it, or set {DB_URL_ENV} to the same value.")
    return None


async def count_legacy(db, col: KeyColumn) -> int:
    """How many rows in *col* still carry the legacy prefix."""
    from sqlalchemy import text

    return int((await db.execute(text(select_legacy_sql(col)))).scalar() or 0)


async def convert(db, col: KeyColumn) -> tuple[int, int]:
    """Converge *col*. Returns ``(updated, dropped)``.

    The collision delete runs first and unconditionally: on a column that can collide
    the UPDATE is what would raise on a duplicate key, and an exception there would
    abandon the columns after it.

    ``dropped`` is reported separately rather than folded into the total because the two
    are different events to an operator reading this once: an updated row kept its state
    under a new key, a dropped one was the losing half of a duplicate pair (see
    ``KeyColumn.keep``). A single number would leave the plan's count unaccounted for
    and look like a bug.
    """
    from sqlalchemy import text

    dropped = 0
    collisions = delete_collisions_sql(col)
    if collisions:
        dropped = int((await db.execute(text(collisions))).rowcount or 0)
    updated = int((await db.execute(text(update_sql(col)))).rowcount or 0)
    return updated, dropped


def print_table(rows: Sequence[tuple[KeyColumn, int]], verb: str) -> None:
    width = max([len(f"{c.table}.{c.column}") for c, _ in rows] + [6])
    print(f"{'column'.ljust(width)}  {verb}")
    for col, n in rows:
        note = f"  — {col.what}" if n else ""
        print(f"{f'{col.table}.{col.column}'.ljust(width)}  {n}{note}")


async def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="perform the conversion (without this, only print the plan)")
    args = ap.parse_args(argv)

    from server.config import get_settings
    from server.db import db_session, init_db
    url = get_settings().db_url
    error = db_url_error(os.environ.get(DB_URL_ENV), url)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(f"database: {url.rsplit('@', 1)[-1]}")  # never the credentials half
    await init_db(url)

    async with db_session() as db:
        plan = [(col, await count_legacy(db, col)) for col in ALL_KEY_COLUMNS]
    print_table(plan, "legacy-prefixed rows")
    total = sum(n for _, n in plan)

    if not args.apply:
        print(f"\n{total} row(s) to converge — re-run with --apply to perform it")
        return 0

    async with db_session() as db:
        results = [(col, *await convert(db, col)) for col in ALL_KEY_COLUMNS]
    print()
    print_table([(col, updated) for col, updated, _ in results], "rows converged")
    dropped = sum(d for _, _, d in results)
    if dropped:
        # Named, not buried: a dropped row's retry counters (or, for a triage twin,
        # its re-detection) are gone, and the plan's count would not otherwise add up.
        print(f"\n{dropped} duplicate row(s) dropped — the other spelling of their "
              f"key already existed (see server/db/brand_keys: KeyColumn.keep)")

    # Re-read rather than trusting the rowcounts: a driver that under-reports would
    # otherwise make a half-done run look complete, and the remedy (re-run) is cheap.
    async with db_session() as db:
        left = sum([await count_legacy(db, col) for col in ALL_KEY_COLUMNS])
    updated = sum(u for _, u, _ in results)
    print(f"\n{updated} row(s) converged, {dropped} dropped, {left} legacy row(s) left")
    return 1 if left else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
