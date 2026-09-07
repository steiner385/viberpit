"""ErrorSink — in-process capture of WARNING+ log records (error-triage spec §4).

A logging.Handler on the root logger folds records into an in-memory map keyed
by fingerprint (server/obs/error_signature.py); a small asyncio task flushes the
map to ``error_signatures`` every FLUSH_INTERVAL_S. A 10 000-record error loop
becomes one upsert with count 10 000.

Guarantees: emit() never blocks on I/O, never raises into the logging call site,
ignores records from this module's own logger, is a no-op when re-entered on
the same thread, and holds at most MAX_PENDING fingerprints between flushes
(overflow is counted in error_sink_dropped_total). A failed flush merges the
batch back — bounded — and retries next interval.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime

from server.obs import metrics
from server.obs.error_signature import OWN_LOGGER, Signature, signature_of

log = logging.getLogger(OWN_LOGGER)
assert log.name == __name__, "OWN_LOGGER must equal this module's logger name"

MAX_PENDING = 512
FLUSH_INTERVAL_S = 5.0
SAMPLE_REFRESH_S = 24 * 3600
RECENT_SESSIONS = 5


def _now() -> datetime:
    from server.db.clock import naive_utc_now
    return naive_utc_now()


@dataclass
class _Pending:
    sig: Signature
    first_seen: datetime
    last_seen: datetime
    count: int = 1
    sessions: list[str] = field(default_factory=list)


class ErrorSink(logging.Handler):
    def __init__(self, *, source: str, host: str) -> None:
        super().__init__(level=logging.WARNING)
        self._source = source
        self._host = host
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._guard = threading.local()
        self._task: asyncio.Task | None = None
        self.dropped = 0

    # -- capture (any thread) ---------------------------------------------------
    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._guard, "active", False) or record.name == OWN_LOGGER:
            return
        self._guard.active = True
        try:
            sig = signature_of(record, source=self._source, host=self._host)
            if sig is None:
                return
            now = _now()
            with self._lock:
                p = self._pending.get(sig.fingerprint)
                if p is None:
                    if len(self._pending) >= MAX_PENDING:
                        self.dropped += 1
                        metrics.error_sink_dropped_total.inc()
                        return
                    p = _Pending(sig=sig, first_seen=now, last_seen=now)
                    self._pending[sig.fingerprint] = p
                else:
                    p.count += 1
                    p.last_seen = now
                if sig.session_id and sig.session_id not in p.sessions:
                    p.sessions.append(sig.session_id)
                    del p.sessions[RECENT_SESSIONS:]
        except Exception:  # noqa: BLE001 — a handler must never raise into the caller
            pass
        finally:
            self._guard.active = False

    def drain(self) -> dict[str, _Pending]:
        with self._lock:
            out, self._pending = self._pending, {}
        return out

    # -- flusher (event loop) ----------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the periodic flusher on the running loop. Idempotent."""
        if self.is_running:
            return
        self._task = asyncio.create_task(self._loop(), name="error-sink-flush")

    async def stop(self) -> None:
        from server.aio import cancel_and_reap
        await cancel_and_reap(self._task, phase="error-sink-flush")
        self._task = None
        await self.flush_once()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_S)
            await self.flush_once()

    async def flush_once(self) -> int:
        """Upsert everything pending. Returns the number of fingerprints flushed."""
        batch = self.drain()
        if not batch:
            return 0
        try:
            await _upsert(batch)
        except Exception:  # noqa: BLE001 — DB trouble must not kill the loop
            log.debug("errsink.flush_failed n=%d", len(batch), exc_info=True)
            with self._lock:
                for fp, p in batch.items():
                    cur = self._pending.get(fp)
                    if cur is not None:
                        cur.count += p.count
                        cur.first_seen = min(cur.first_seen, p.first_seen)
                    elif len(self._pending) < MAX_PENDING:
                        self._pending[fp] = p
                    else:
                        self.dropped += 1
                        metrics.error_sink_dropped_total.inc()
            return 0
        return len(batch)


async def _upsert(batch: dict[str, _Pending]) -> None:
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import ErrorSignature

    async with db_session() as db:
        existing = {
            r.fingerprint: r for r in (await db.execute(
                select(ErrorSignature).where(ErrorSignature.fingerprint.in_(list(batch)))
            )).scalars()
        }
        for fp, p in batch.items():
            row = existing.get(fp)
            if row is None:
                db.add(ErrorSignature(
                    fingerprint=fp, source=p.sig.source, level=p.sig.level,
                    logger=p.sig.logger, event=p.sig.event, title=p.sig.title,
                    sample_message=p.sig.sample_message, sample_exc=p.sig.sample_exc,
                    sample_context={**p.sig.context, "recent_sessions": list(p.sessions)},
                    sample_at=p.last_seen, count=p.count, count_since_filed=p.count,
                    status="observed", first_seen_at=p.first_seen, last_seen_at=p.last_seen,
                ))
                continue
            row.count = (row.count or 0) + p.count
            row.count_since_filed = (row.count_since_filed or 0) + p.count
            row.last_seen_at = p.last_seen
            ctx = dict(row.sample_context or {})
            recent = list(p.sessions) + [s for s in ctx.get("recent_sessions", [])
                                         if s not in p.sessions]
            ctx["recent_sessions"] = recent[:RECENT_SESSIONS]
            stale = row.sample_at is None or \
                (p.last_seen - row.sample_at).total_seconds() >= SAMPLE_REFRESH_S
            if stale:
                row.sample_message = p.sig.sample_message
                row.sample_exc = p.sig.sample_exc
                row.sample_at = p.last_seen
                ctx.update({k: v for k, v in p.sig.context.items() if v is not None})
            row.sample_context = ctx  # reassign: JSON columns don't track in-place edits


# -- process-wide singleton -------------------------------------------------------
_sink: ErrorSink | None = None


def install_error_sink(*, source: str) -> ErrorSink:
    """Create the process's sink and attach it to the root logger. Idempotent."""
    global _sink
    if _sink is not None:
        return _sink
    try:
        from server.hostname import self_host_name
        host = self_host_name()
    except Exception:  # noqa: BLE001 — a hostname lookup must not block logging setup
        host = "unknown"
    _sink = ErrorSink(source=source, host=host)
    logging.getLogger().addHandler(_sink)
    return _sink


def get_error_sink() -> ErrorSink | None:
    return _sink


def reset_for_tests() -> None:
    global _sink
    if _sink is not None:
        logging.getLogger().removeHandler(_sink)
    _sink = None
