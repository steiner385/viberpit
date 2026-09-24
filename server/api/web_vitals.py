"""Field Core Web Vitals ingest and summary (#1411).

`POST /api/web-vitals` takes a batch from `web/src/obs/vitals.ts`;
`GET /api/web-vitals/summary` turns the stored rows into the p75s the issue's
targets are stated in, overall and segmented by route / device / browser.

The arithmetic and the thresholds live in `server/obs/web_vitals.py` — this
module is storage, validation and shape.

## Validation is the privacy boundary

The browser is asked never to send a URL, an identity or a session id, and
`obs/routeTemplate.ts` is careful about it. That is not a guarantee: anything on
the tailnet can POST here. So every field is re-checked on arrival — the metric
against the known set, the route re-scrubbed through the same rules the client
applies, everything else against a fixed vocabulary — and the User-Agent is
reduced to a family and dropped. A field that survives ingest is a field that
could not carry a secret.
"""
from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import select as sa_select

from server.api.ratelimit import web_vitals_rate_limit
from server.auth.middleware import current_identity
from server.config import get_settings
from server.db import db_session
from server.db.models import WebVitalSample
from server.obs.web_vitals import (
    CORE_WEB_VITALS,
    MIN_SAMPLES_FOR_VERDICT,
    SEGMENTS,
    WEB_VITAL_METRICS,
    WEB_VITAL_THRESHOLDS,
    Sample,
    browser_family,
    summarize,
    summarize_by,
)

router = APIRouter(prefix="/api/web-vitals", tags=["web-vitals"])

log = logging.getLogger(__name__)

# A page load reports five metrics; a long-lived kiosk tab flushes repeatedly.
# 60 leaves generous headroom without letting one POST carry a day of data.
MAX_SAMPLES = 60
MAX_ROUTE_LEN = 120
# Beyond this a "measurement" is a bug or a fabrication: 10 minutes of LCP, or a
# CLS of 1000. Clamping would invent a plausible number, so these are rejected.
MAX_VALUE = 600_000.0
# The vocabulary each dimension is allowed to take, so an attacker cannot mint
# unbounded segment keys and blow up the summary's cardinality.
DEVICES = frozenset({"phone", "tablet", "desktop", "wide"})
# Every value `web-vitals` reports for `navigationType`, kept apart because they
# are genuinely different experiences: a `back-forward-cache` restore is the
# fastest load the browser can produce and pooling it with a cold `navigate`
# flatters the cold number and punishes the warm one.
NAV_TYPES = frozenset({"navigate", "reload", "back-forward", "back_forward",
                       "back-forward-cache", "prerender", "restore"})
MAX_WINDOW_HOURS = 24 * 90
# The summary segments by three dimensions at once, which is far easier to fold in
# Python than in portable SQL — but it means the window's rows are materialised.
# A 90-day window on a busy edge is hundreds of thousands of rows, so the read is
# capped at the most recent N and the answer says when it was. Silently summarising
# a truncated window would be the worst outcome: a p75 that looks authoritative and
# is quietly about a different period than the one asked for.
MAX_SUMMARY_ROWS = 100_000

_ROUTE_SEGMENT_OK = re.compile(r"^[A-Za-z0-9:._~…-]{0,64}$")
# A build sha and a page-load uuid are both drawn from this alphabet. Anything
# else in those fields did not come from our client, and the endpoint is
# reachable by anything on the tailnet.
_TOKEN_OK = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Opportunistic retention sweep: at most once an hour per process, on the ingest
# path. A dedicated background loop would be a second thing to supervise for a
# DELETE that takes milliseconds, and the table only grows when ingest runs.
_PRUNE_INTERVAL_S = 3600.0
_last_prune_at = 0.0


class VitalSampleIn(BaseModel):
    metric: str
    value: float
    route: str = "/"
    device: str = "unknown"


class VitalPageIn(BaseModel):
    navType: str = "navigate"
    appVersion: str | None = None
    clientId: str | None = None
    lazyChunks: int = 0
    lazyChunkBytes: int = 0


class VitalBatchIn(BaseModel):
    samples: list[VitalSampleIn] = Field(default_factory=list)
    page: VitalPageIn = Field(default_factory=VitalPageIn)


def _clean_route(route: str) -> str:
    """Re-apply the client's redaction rules server-side.

    Deliberately a re-check rather than a trust: the client is the thing whose
    correctness this is protecting against. Anything unrecognisable collapses to
    `/other` instead of being stored — an unparseable route is worthless as a
    segment key and is exactly the shape a smuggled URL would arrive in.
    """
    route = route.split("?", 1)[0].split("#", 1)[0][:MAX_ROUTE_LEN]
    if not route.startswith("/"):
        route = "/" + route
    segments = [s for s in route.split("/") if s]
    if not segments:
        return "/"
    out: list[str] = []
    for seg in segments[:6]:
        if not _ROUTE_SEGMENT_OK.match(seg):
            return "/other"
        # A segment carrying a digit is an identifier the client should already
        # have replaced; replace it here too rather than storing it.
        out.append(seg if seg.startswith(":") or not any(c.isdigit() for c in seg) else ":id")
    return "/" + "/".join(out)


def _safe_token(value: str | None) -> str | None:
    """A provenance field, or None — never a value of the caller's choosing.

    `appVersion` and `clientId` are only ever read back as grouping keys, so a
    value that cannot be one is worth nothing and is dropped whole rather than
    sanitised into something that looks legitimate.
    """
    if not value:
        return None
    return value if _TOKEN_OK.match(value) else None


def _naive_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _prune_expired(now: float) -> None:
    global _last_prune_at
    if now - _last_prune_at < _PRUNE_INTERVAL_S:
        return
    _last_prune_at = now
    days = max(1, get_settings().web_vitals_retention_days)
    cutoff = _naive_utc_now() - timedelta(days=days)
    try:
        async with db_session() as db:
            await db.execute(
                sa_delete(WebVitalSample).where(WebVitalSample.created_at < cutoff))
            await db.commit()
    except Exception:  # noqa: BLE001 -- a failed sweep must never fail an ingest
        log.info("web-vitals: retention sweep failed", exc_info=True)


@router.post("", status_code=202, dependencies=[Depends(web_vitals_rate_limit)])
async def ingest(
    batch: VitalBatchIn,
    _identity: str = Depends(current_identity),
    user_agent: str | None = Header(default=None),
) -> dict:
    """Store a batch of field samples. 202 even when every sample is dropped:
    the browser cannot act on a rejection and must not retry into a loop."""
    settings = get_settings()
    if not settings.web_vitals_enabled:
        # The kill switch stops the endpoint too, not just the collector — a
        # stale bundle in an open tab keeps reporting for as long as it lives.
        return {"accepted": 0, "enabled": False}
    if len(batch.samples) > MAX_SAMPLES:
        raise HTTPException(status_code=413, detail="too many samples")

    browser = browser_family(user_agent)
    nav_type = batch.page.navType if batch.page.navType in NAV_TYPES else "other"
    app_version = _safe_token(batch.page.appVersion)
    client_id = _safe_token(batch.page.clientId)
    lazy_chunks = max(0, min(batch.page.lazyChunks, 10_000))
    lazy_chunk_bytes = max(0, min(batch.page.lazyChunkBytes, 2**40))
    now = _naive_utc_now()

    rows: list[WebVitalSample] = []
    for s in batch.samples:
        if s.metric not in WEB_VITAL_METRICS:
            continue
        if not math.isfinite(s.value) or s.value < 0 or s.value > MAX_VALUE:
            continue
        rows.append(WebVitalSample(
            metric=s.metric,
            value=float(s.value),
            route=_clean_route(s.route),
            device=s.device if s.device in DEVICES else "unknown",
            browser=browser,
            nav_type=nav_type,
            app_version=app_version,
            client_id=client_id,
            lazy_chunks=lazy_chunks,
            lazy_chunk_bytes=lazy_chunk_bytes,
            created_at=now,
        ))

    if rows:
        async with db_session() as db:
            db.add_all(rows)
            await db.commit()

    await _prune_expired(time.monotonic())
    return {"accepted": len(rows)}


@router.get("/summary")
async def summary(
    _identity: str = Depends(current_identity),
    hours: int = Query(default=24, ge=1, le=MAX_WINDOW_HOURS),
    route: str | None = Query(default=None),
) -> dict:
    """p75 per metric over the window: overall, then by route, device and browser.

    `route` narrows every figure to one route shape, which is how you answer "is
    the Roadmap page the slow one, or is everything slow".
    """
    settings = get_settings()
    since = _naive_utc_now() - timedelta(hours=hours)

    stmt = sa_select(
        WebVitalSample.metric, WebVitalSample.value, WebVitalSample.route,
        WebVitalSample.device, WebVitalSample.browser,
    ).where(WebVitalSample.created_at >= since)
    if route:
        stmt = stmt.where(WebVitalSample.route == _clean_route(route))
    stmt = stmt.order_by(WebVitalSample.created_at.desc()).limit(MAX_SUMMARY_ROWS)

    async with db_session() as db:
        raw = (await db.execute(stmt)).all()

    samples = [
        Sample(metric=m or "", value=float(v or 0.0), route=r or "/",
               device=d or "unknown", browser=b or "unknown")
        for m, v, r, d, b in raw
    ]

    return {
        "windowHours": hours,
        "route": route,
        "sampleCount": len(samples),
        # True when the window held more than MAX_SUMMARY_ROWS: the figures below
        # are the most recent slice of it, not the whole thing.
        "truncated": len(samples) >= MAX_SUMMARY_ROWS,
        "enabled": settings.web_vitals_enabled,
        # Echoed so a dashboard renders the same bands the verdicts were computed
        # from instead of restating them (constitution VI).
        "thresholds": {m: {"good": g, "poor": p} for m, (g, p) in WEB_VITAL_THRESHOLDS.items()},
        "coreMetrics": list(CORE_WEB_VITALS),
        "minSamplesForVerdict": MIN_SAMPLES_FOR_VERDICT,
        "overall": summarize(samples),
        "byRoute": summarize_by(samples, "route"),
        "byDevice": summarize_by(samples, "device"),
        "byBrowser": summarize_by(samples, "browser"),
        "segments": list(SEGMENTS),
    }
