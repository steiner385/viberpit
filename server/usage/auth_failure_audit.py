"""Durable audit of needs-reauth flips emitted by the DB-free core.

The core (ccswitch.accounts) calls a sink with an AuthFailure at each flip; the
sink (registered here) enqueues it, and an async drainer writes an
`account.needs_reauth` AuditEvent through the redaction layer. Decoupling via a
thread-safe queue handles the sync-worker-thread → async-DB boundary cleanly.
"""
from __future__ import annotations

import asyncio
import logging
import queue
from typing import Any, Callable

from ccswitch.accounts import AuthFailure, set_auth_failure_sink
from server.audit import record_audit

log = logging.getLogger("server")


def build_needs_reauth_detail(f: AuthFailure) -> dict[str, Any]:
    return {
        "account": f.account, "status": f.status, "body": f.body,
        "path": f.path, "token_expires_at": f.token_expires_at, "attempts": f.attempts,
    }


class AuthFailureAuditor:
    def __init__(self, *, identity: str, session_factory: Callable[[], Any]):
        self._identity = identity
        self._session_factory = session_factory
        self._q: "queue.Queue[AuthFailure]" = queue.Queue(maxsize=256)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        set_auth_failure_sink(self._enqueue)
        self._task = asyncio.create_task(self._drain())

    def stop(self) -> None:
        set_auth_failure_sink(None)
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _enqueue(self, f: AuthFailure) -> None:
        # Called from a worker thread (poller). Never block / raise into the core.
        try:
            self._q.put_nowait(f)
        except queue.Full:
            log.warning("auth_failure_audit: queue full, dropping needs-reauth event for %s", f.account)

    async def _drain(self) -> None:
        while True:
            try:
                f = await asyncio.to_thread(self._q.get, True, 1.0)  # block=True, timeout=1.0s
            except queue.Empty:
                continue
            except asyncio.CancelledError:
                raise
            try:
                await self._write(f)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("auth_failure_audit: drainer write failed", exc_info=True)

    async def _write(self, f: AuthFailure) -> None:
        async with self._session_factory() as db:
            await record_audit(db, "account.needs_reauth", self._identity, build_needs_reauth_detail(f))
            await db.commit()
