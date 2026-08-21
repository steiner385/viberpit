#!/usr/bin/env python3
"""Backfill Session.archive_root for sessions archived before the column existed.

Sessions archived under a previous CCSWITCH_ARCHIVE_HOME have archive_root NULL, so
unarchive resolves them against the *current* archive home, misses, and (since the
loud-failure change) raises ArchiveMissingError. This stamps each such row with the
root its bytes actually live in.

Optionally CONSOLIDATES: with --target-root, each session directory is moved to the
target first and only then stamped, so an interrupted run leaves every row pointing at
wherever its bytes really are. The job is resumable — re-running skips what it did.

Dry-run by default; pass --apply to write.

  # stamp in place (no data movement)
  ccswitch-backfill-archive-root.py --legacy-root /mnt/ssk-ssd/sessions-archive --apply

  # consolidate onto the current archive disk, then stamp
  ccswitch-backfill-archive-root.py --legacy-root /mnt/ssk-ssd/sessions-archive \
      --target-root /mnt/hdd-storage/ccswitch-archives --apply
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--legacy-root", required=True, type=Path,
                    help="archive root these sessions were actually written to")
    ap.add_argument("--target-root", type=Path, default=None,
                    help="if given, MOVE each session directory here before stamping")
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = ap.parse_args()

    legacy: Path = args.legacy_root
    if not legacy.is_dir():
        print(f"error: --legacy-root {legacy} is not a directory", file=sys.stderr)
        return 2
    target: Path | None = args.target_root
    if target is not None and args.apply:
        target.mkdir(parents=True, exist_ok=True)

    from sqlalchemy import select as sa_select, update as sa_update

    from server.db import db_session, init_db
    from server.db.models import Session as DbSession

    await init_db()

    on_disk = {p.name for p in legacy.iterdir() if p.is_dir()}
    if not on_disk:
        print(f"no session directories under {legacy}")
        return 0

    async with db_session() as db:
        rows = (await db.execute(
            sa_select(DbSession.id, DbSession.status, DbSession.archive_root)
            .where(DbSession.id.in_(on_disk))
        )).all()

    by_id = {r.id: r for r in rows}
    todo = [sid for sid in sorted(on_disk)
            if sid in by_id and by_id[sid].archive_root is None]
    already = [sid for sid in on_disk if sid in by_id and by_id[sid].archive_root is not None]
    unknown = sorted(on_disk - set(by_id))

    root_for = str(target) if target is not None else str(legacy)
    verb = "move+stamp" if target is not None else "stamp"
    print(f"  on disk under {legacy}: {len(on_disk)}")
    print(f"  matching session rows : {len(by_id)}")
    print(f"  already stamped       : {len(already)} (skipped)")
    print(f"  no DB row             : {len(unknown)} (left on disk, nothing to stamp)")
    print(f"  to {verb:<10}        : {len(todo)} -> archive_root={root_for}")
    if not args.apply:
        print("\n  DRY RUN — re-run with --apply to write")
        return 0

    moved = stamped = failed = 0
    for sid in todo:
        src = legacy / sid
        try:
            if target is not None:
                dest = target / sid
                if dest.exists():
                    print(f"  skip {sid[:8]}: destination already exists", file=sys.stderr)
                    continue
                # Move the bytes BEFORE stamping: an interrupted run must never leave a
                # row pointing at a root its directory is not in.
                shutil.move(str(src), str(dest))
                moved += 1
            async with db_session() as db:
                await db.execute(sa_update(DbSession)
                                 .where(DbSession.id == sid)
                                 .values(archive_root=root_for))
            stamped += 1
        except Exception as exc:  # keep going — each session is independent
            failed += 1
            print(f"  FAILED {sid}: {exc}", file=sys.stderr)

    print(f"\n  moved={moved} stamped={stamped} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
