"""Merged-PR timeline store (SPEC §2 — PRTIMELINE CAPTURE substrate).

One row per merged PR in ``merged_prs``, carrying waypoint timestamps that feed
the deploy-chain (§3) and lead-time metrics. All timestamps are stored trailing-Z
ISO-8601 so lexicographic windowing (retention) holds — GitHub delivers Z-form,
and ``datetime.isoformat()`` callers (+00:00) are normalized via ``_z_form``
(same convention as ``group_history``).

Store functions:

- ``upsert_merged_pr``: INSERT a new row (merged_at ALWAYS set — writer-side
  invariant), else UPDATE only columns currently NULL (first-value-wins /
  COALESCE(excluded.X, X)).
- ``mark_env_live``: set qa_live_at / prod_live_at only when NULL (idempotent).
- ``timelines_for``: camelCase PrTimeline dicts for the payload (Task 6 wiring).
- ``tracked_merged``: rows merged within the retention window (default 7 days).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from server.db import db_session
from server.db.models import MergedPr

# Nullable columns that follow first-value-wins semantics on upsert. merged_at is
# excluded: it's the writer-side invariant, always set on insert, never re-tried.
_FIRST_WINS_COLS = (
    "title",
    "url",
    "merge_commit_sha",
    "created_at",
    "first_green_at",
    "enqueued_at",
    "qa_live_at",
    "prod_live_at",
)


def _z_form(iso: str | None) -> str | None:
    """Normalize a UTC ISO timestamp to trailing-Z form (see group_history)."""
    if iso is None:
        return None
    return iso.replace("+00:00", "Z")


async def upsert_merged_pr(
    repo: str,
    number: int,
    *,
    merged_at: str,
    title: str | None = None,
    url: str | None = None,
    merge_commit_sha: str | None = None,
    created_at: str | None = None,
    first_green_at: str | None = None,
    enqueued_at: str | None = None,
    qa_live_at: str | None = None,
    prod_live_at: str | None = None,
) -> None:
    """Insert a merged-PR row, or fill only its still-NULL columns.

    First value wins for every nullable column: an INSERT sets everything
    supplied; a subsequent upsert of the same (repo, number) writes a column
    ONLY if the existing row's value is NULL — matching plotroom's
    COALESCE(excluded.X, X). ``merged_at`` is a writer-side invariant (always
    passed) and is set on insert; on conflict it is left untouched.
    """
    values = {
        "repo": repo,
        "number": number,
        "merged_at": _z_form(merged_at),
        "title": title,
        "url": url,
        "merge_commit_sha": merge_commit_sha,
        "created_at": _z_form(created_at),
        "first_green_at": _z_form(first_green_at),
        "enqueued_at": _z_form(enqueued_at),
        "qa_live_at": _z_form(qa_live_at),
        "prod_live_at": _z_form(prod_live_at),
    }
    stmt = sqlite_insert(MergedPr).values(**values)
    # COALESCE(existing, excluded): keep the stored value if present, else adopt
    # the new one — so the first non-NULL write wins per column.
    set_ = {
        col: func.coalesce(getattr(MergedPr, col), getattr(stmt.excluded, col))
        for col in _FIRST_WINS_COLS
    }
    stmt = stmt.on_conflict_do_update(index_elements=["repo", "number"], set_=set_)
    async with db_session() as db:
        await db.execute(stmt)


async def mark_env_live(repo: str, number: int, env: str, at_iso: str) -> None:
    """Record when a merged PR's commit went live in ``env`` (idempotent).

    ``env`` 'prod'/'production' → prod_live_at, else qa_live_at. Writes only
    when the target column is currently NULL (first observation wins), so
    repeated ancestry hits never overwrite the earliest live timestamp.
    """
    col = "prod_live_at" if env in ("prod", "production") else "qa_live_at"
    target = getattr(MergedPr, col)
    stmt = (
        update(MergedPr)
        .where(
            MergedPr.repo == repo,
            MergedPr.number == number,
            target.is_(None),
        )
        .values({col: _z_form(at_iso)})
    )
    async with db_session() as db:
        await db.execute(stmt)


async def timelines_for(repo: str, numbers: list[int]) -> dict[int, dict]:
    """camelCase PrTimeline dicts keyed by PR number (SPEC §2 payload shape).

    Each value: {createdAt, firstGreenAt, enqueuedAt, mergedAt, qaLiveAt,
    prodLiveAt} — ISO-8601 strings or None. Only rows present in ``merged_prs``
    for ``repo`` and in ``numbers`` are returned.
    """
    if not numbers:
        return {}
    stmt = select(MergedPr).where(
        MergedPr.repo == repo, MergedPr.number.in_(numbers)
    )
    async with db_session() as db:
        rows = (await db.execute(stmt)).scalars().all()
    return {
        row.number: {
            "createdAt": row.created_at,
            "firstGreenAt": row.first_green_at,
            "enqueuedAt": row.enqueued_at,
            "mergedAt": row.merged_at,
            "qaLiveAt": row.qa_live_at,
            "prodLiveAt": row.prod_live_at,
        }
        for row in rows
    }


async def tracked_merged(
    repo: str,
    retention_days: int = 7,
    now: datetime | None = None,
) -> list[MergedPr]:
    """Merged-PR rows for ``repo`` within the retention window, newest first.

    "Merged recently enough" = merged_at >= now - retention_days. Comparison is
    lexicographic over the stored Z-form ISO strings; ``since`` is computed in
    Z-form so it sorts consistently against the stored timestamps.
    """
    now = now or datetime.now(timezone.utc)
    since = _z_form((now - timedelta(days=retention_days)).isoformat())
    stmt = (
        select(MergedPr)
        .where(MergedPr.repo == repo, MergedPr.merged_at >= since)
        .order_by(MergedPr.merged_at.desc())
    )
    async with db_session() as db:
        return list((await db.execute(stmt)).scalars().all())
