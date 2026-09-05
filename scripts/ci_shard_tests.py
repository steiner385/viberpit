#!/usr/bin/env python3
"""Bucket server/tests/ files across a fixed number of CI shard jobs.

`lint-and-test`'s `server/tests/` step was 87% of the job's 742s wall time
because the runner only has 2 vCPUs (`-n auto` resolves to 2 workers, not the
~7x the old comment claimed — see #1137). Splitting whole *files* — never
individual tests — across N shard jobs keeps `--dist loadfile` semantics
intact per shard: tests that share DB/Postgres state (e.g.
test_postgres_compat) still land on one worker, just within a shard job
instead of within the single runner.

The bucket is a hash of the file's repo-relative path, so it needs no
maintained duration data and doesn't drift as tests are added or removed —
it isn't perfectly balanced by runtime, but it is stable and self-updating.

Usage:
    python3 scripts/ci_shard_tests.py --shard 1 --total 4
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _discover(root: Path) -> list[Path]:
    return sorted(root.rglob("test_*.py"))


def _bucket(path: Path, total_shards: int) -> int:
    digest = hashlib.sha256(str(path).encode()).hexdigest()
    return int(digest, 16) % total_shards


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="server/tests")
    parser.add_argument("--shard", type=int, required=True, help="1-indexed shard number")
    parser.add_argument("--total", type=int, required=True, help="total shard count")
    args = parser.parse_args(argv)

    if not 1 <= args.shard <= args.total:
        print(f"::error::--shard {args.shard} out of range for --total {args.total}", file=sys.stderr)
        return 1

    root = _REPO_ROOT / args.root
    files = _discover(root)
    if not files:
        print(f"::error::no test files found under {root}", file=sys.stderr)
        return 1

    shard_index = args.shard - 1
    selected = [f.relative_to(_REPO_ROOT) for f in files if _bucket(f.relative_to(_REPO_ROOT), args.total) == shard_index]
    if not selected:
        print(f"::error::shard {args.shard}/{args.total} selected zero of {len(files)} files", file=sys.stderr)
        return 1

    print(" ".join(str(f) for f in selected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
