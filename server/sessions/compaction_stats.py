"""Global rolling estimate of how long context auto-compaction takes.

Each compaction yields a real wall-clock duration (PreCompact hook → compact_boundary).
We fold it into a persisted EWMA on the AppSettings singleton and serve the current
estimate so the UI can animate a progress bar against an expected duration. There is no
real compaction-progress signal — this is a learned *estimate*, clamped to a sane range.
"""
from __future__ import annotations

from sqlalchemy import select

from server.db import db_session
from server.db.models import AppSettings

EWMA_ALPHA = 0.3
DEFAULT_ESTIMATE_MS = 12000.0
MIN_MS = 3000.0
MAX_MS = 60000.0


def fold(prev_ewma: float | None, sample_ms: float) -> float:
    """Pure EWMA fold. Clamp the sample to [MIN_MS, MAX_MS]; seed with it when cold."""
    sample = max(MIN_MS, min(MAX_MS, sample_ms))
    if prev_ewma is None:
        return sample
    return EWMA_ALPHA * sample + (1 - EWMA_ALPHA) * prev_ewma


async def _get_or_create(db) -> AppSettings:
    row = (await db.execute(select(AppSettings).where(AppSettings.id == "singleton"))).scalar_one_or_none()
    if row is None:
        row = AppSettings(id="singleton")
        db.add(row)
    return row


async def record_duration(sample_ms: float) -> None:
    """Fold one measured compaction duration into the persisted EWMA.

    ``db_session()`` commits on clean exit, so no explicit commit is needed."""
    async with db_session() as db:
        row = await _get_or_create(db)
        row.compaction_ewma_ms = fold(row.compaction_ewma_ms, sample_ms)
        row.compaction_samples = (row.compaction_samples or 0) + 1


async def current_estimate() -> float:
    """Return the learned estimate (ms), or DEFAULT_ESTIMATE_MS when cold."""
    async with db_session() as db:
        row = (await db.execute(select(AppSettings).where(AppSettings.id == "singleton"))).scalar_one_or_none()
        if row is None or row.compaction_ewma_ms is None:
            return DEFAULT_ESTIMATE_MS
        return row.compaction_ewma_ms
