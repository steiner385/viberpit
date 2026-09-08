"""Durable store for the SDLC triage queue (#804).

The ONE place ``triage_items`` rows are written. The control-band monitor's
orchestrator seams, the ``/api/sdlc/triage`` routes and the SDLC Intake trigger
seam all read and write through here, so "what queueing an incident means" cannot
drift between the automation that does it and the API that undoes it
(constitution VI).

Everything above this layer is pure: ``server.sdlc.control_bands`` decides *that*
a breach happened and ``server.sdlc.bands`` decides what tier answers it. This
module only persists the result.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select

from server.db import db_session
from server.db.models import TRIAGE_TERMINAL_STATUSES, TriageItem


@dataclass(frozen=True)
class FiledItem:
    """The monitor loop's view of an upserted row — mirrors
    ``server.automations.control_band_monitor.FiledItem``, which is the type the
    loop's seam is declared with. Re-stated here rather than imported so the
    store does not depend on an automation module; the loop constructs its own
    from these fields."""
    id: str
    key: str
    status: str
    created: bool


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def item_to_dict(item: TriageItem) -> dict:
    """Wire form of a triage row — the single serializer, used by the list route
    and by the action routes' echo so a mutation's response can never describe a
    row differently from the way the queue that follows it does."""
    return {
        "id": item.id,
        "key": item.key,
        "metric": item.metric,
        "source": item.source,
        "repo": item.repo,
        "rule": item.rule,
        "sigma": item.sigma,
        "side": item.side,
        "tier": item.tier,
        "response": item.response,
        "value": item.value,
        "baselineMean": item.baseline_mean,
        "baselineStddev": item.baseline_stddev,
        "baselineN": item.baseline_n,
        "observedAt": item.observed_at,
        "summary": item.summary,
        "evidence": item.evidence or [],
        "status": item.status,
        "dismissedReason": item.dismissed_reason,
        "scheduledFor": item.scheduled_for.isoformat() if item.scheduled_for else None,
        "sessionId": item.session_id,
        "initiativeSlug": item.initiative_slug,
        "occurrences": item.occurrences,
        "createdAt": item.created_at.isoformat() if item.created_at else None,
        "updatedAt": item.updated_at.isoformat() if item.updated_at else None,
    }


async def upsert_breach(*, key: str, metric: str, source: str, repo: str,
                        rule: str, sigma: int, side: str, tier: str, response: str,
                        value: float, baseline_mean: float, baseline_stddev: float,
                        baseline_n: int, observed_at: str, summary: str,
                        evidence: list[dict], slug: str) -> FiledItem:
    """File a breach, or refresh the row that already holds it. Idempotent by *key*.

    Returns the status the row had **before** this call, because that — not the
    status after — is what decides whether the monitor may act: an item a human
    already dismissed must stay dismissed even as the breach that produced it
    goes on breaching.

    ``initiative_slug`` is stamped only on insert. A persisting incident keeps
    the initiative name it was first filed under, so re-observation on a later
    day cannot silently re-point a queued item at a second initiative.
    """
    async with db_session() as db:
        row = (await db.execute(
            select(TriageItem).where(TriageItem.key == key))).scalar_one_or_none()
        if row is None:
            row = TriageItem(
                key=key, metric=metric, source=source, repo=repo, rule=rule,
                sigma=sigma, side=side, tier=tier, response=response, value=value,
                baseline_mean=baseline_mean, baseline_stddev=baseline_stddev,
                baseline_n=baseline_n, observed_at=observed_at, summary=summary,
                evidence=evidence, status="open", initiative_slug=slug,
                occurrences=1)
            db.add(row)
            await db.flush()
            return FiledItem(id=row.id, key=key, status="open", created=True)

        previous = row.status or "open"
        # Refresh the measurements even on a terminal row: a dismissed incident
        # that is still breaching should show its CURRENT numbers when someone
        # goes back to look at why it was dismissed.
        row.value = value
        row.baseline_mean = baseline_mean
        row.baseline_stddev = baseline_stddev
        row.baseline_n = baseline_n
        row.observed_at = observed_at
        row.summary = summary
        row.evidence = evidence
        row.tier = tier
        row.response = response
        if previous not in TRIAGE_TERMINAL_STATUSES:
            row.occurrences = (row.occurrences or 1) + 1
        await db.flush()
        return FiledItem(id=row.id, key=key, status=previous, created=False)


async def set_disposition(item_id: str, status: str,
                          session_id: str | None = None) -> None:
    """Move an item to *status*, optionally attaching the session that owns it."""
    async with db_session() as db:
        row = (await db.execute(
            select(TriageItem).where(TriageItem.id == item_id))).scalar_one_or_none()
        if row is None:
            return
        row.status = status
        if session_id is not None:
            row.session_id = session_id
        await db.flush()


async def count_queued() -> int:
    """How many incidents are waiting for SDLC Intake right now."""
    async with db_session() as db:
        return int((await db.execute(
            select(func.count()).select_from(TriageItem)
            .where(TriageItem.status == "queued"))).scalar_one() or 0)


async def release_due(now: datetime | None = None) -> int:
    """Return scheduled items whose time has come to the ``open`` queue.

    Without this, "schedule for later" would be a black hole: the monitor never
    re-drives a dispositioned item, so a deferred incident would rest as
    ``scheduled`` forever. Returns how many were released.
    """
    at = now or _utcnow()
    async with db_session() as db:
        rows = (await db.execute(
            select(TriageItem).where(TriageItem.status == "scheduled"))).scalars().all()
        released = 0
        for row in rows:
            due = row.scheduled_for
            if due is None:
                continue
            # Stored naive-UTC on some dialects; compare in one frame of reference.
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            if due <= at:
                row.status = "open"
                row.scheduled_for = None
                released += 1
        if released:
            await db.flush()
        return released


async def list_items(*, statuses: list[str] | None = None,
                     limit: int = 100) -> list[TriageItem]:
    """Queue contents, newest first. ``statuses`` filters; ``None`` returns all."""
    stmt = select(TriageItem).order_by(TriageItem.created_at.desc()).limit(limit)
    if statuses:
        stmt = stmt.where(TriageItem.status.in_(statuses))
    async with db_session() as db:
        return list((await db.execute(stmt)).scalars().all())


async def queued_for_intake(limit: int = 5) -> list[TriageItem]:
    """Incidents SDLC Intake should turn into ``intent.md`` PRs, oldest first.

    Oldest-first on purpose, unlike ``list_items``: this is a work queue, and the
    incident that has been waiting longest goes next.
    """
    async with db_session() as db:
        return list((await db.execute(
            select(TriageItem)
            .where(TriageItem.status == "queued")
            .order_by(TriageItem.created_at)
            .limit(limit))).scalars().all())


async def mark_intake(slug: str, session_id: str | None = None) -> None:
    """Record that Intake has adopted the incident filed under *slug*.

    Called from the intake record-intent seam — the moment an intent artifact row
    exists is the moment the incident stops being a queue item and becomes an
    Initiative. ``intake`` is terminal for the monitor, so the item is never
    re-queued even while the underlying metric keeps breaching.
    """
    async with db_session() as db:
        rows = (await db.execute(select(TriageItem).where(
            TriageItem.initiative_slug == slug,
            TriageItem.status == "queued"))).scalars().all()
        for row in rows:
            row.status = "intake"
            if session_id is not None:
                row.session_id = session_id
        if rows:
            await db.flush()
