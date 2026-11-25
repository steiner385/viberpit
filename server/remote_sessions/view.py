"""The edge's live view of one watched remote session, for the ``/ws`` attach.

``open_remote_session`` (in process: ``facade``; remote: ``RemoteOrchestrator``) builds
one from the watcher's snapshot and a live subscription that was joined BEFORE the
snapshot was taken, so nothing falls in between: live events at or below the snapshot's
``head`` are the snapshot's own and are dropped. It has the shape ``ws._forward_live``
drives (``events()`` yielding objects with ``to_wire()`` and ``src_seq``, plus
``close()``), so the attach reuses the ordinary forwarder.

While it is iterated it touches the watcher's lease every ``TOUCH_S``. If the watcher
stopped or restarted underneath it (a different epoch), the view ends with one
``remote_stream_reset`` error frame: the client re-attaches and gets a fresh snapshot.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from server.aio import cancel_and_reap
from server.remote_sessions.service import RemoteSessionsError
from server.sessions import events as ev

log = logging.getLogger(__name__)

TOUCH_S = 30.0
_META = "_remote"
RESET_CODE = "remote_stream_reset"
RESET_MESSAGE = "The remote session's live view restarted. Reopen it to catch up."


def strip(wire: dict) -> dict:
    """*wire* without the service's ``_remote`` stamp (a plain dict copy)."""
    return {k: v for k, v in wire.items() if k != _META}


class _Event:
    """A wire dict that ``ws._forward_live`` can ``to_wire()``; ``src_seq`` is the daemon
    bus seq when the event came over the bus (srcSeq telemetry), else None."""
    __slots__ = ("_wire", "src_seq")

    def __init__(self, wire: dict, src_seq: int | None = None) -> None:
        self._wire = wire
        self.src_seq = src_seq

    def to_wire(self) -> dict:
        return dict(self._wire)


class RemoteSessionView:
    def __init__(self, snapshot: dict, live: AsyncIterator[dict],
                 release: Callable[[], None],
                 touch: Callable[[], Awaitable[dict]]) -> None:
        self.epoch = snapshot.get("epoch")
        self.head = snapshot.get("head", -1)
        raw = snapshot.get("events")
        self.snapshot: list[dict] = [strip(e) for e in raw if isinstance(e, dict)] \
            if isinstance(raw, list) else []
        self._live = live
        self._release = release
        self._touch = touch
        self._closed = False

    def close(self) -> None:
        """Release the live subscription. Idempotent."""
        if not self._closed:
            self._closed = True
            self._release()

    async def _touch_loop(self, q: asyncio.Queue) -> None:
        while True:
            await asyncio.sleep(TOUCH_S)
            try:
                reply = await self._touch()
            except RemoteSessionsError:
                q.put_nowait(("reset", None))
                return
            except Exception:  # noqa: BLE001 — a missed touch is retried next tick
                log.debug("remote sessions: touch failed", exc_info=True)
                continue
            if not isinstance(reply, dict) or reply.get("epoch") != self.epoch:
                q.put_nowait(("reset", None))
                return

    async def _pump(self, q: asyncio.Queue) -> None:
        try:
            async for wire in self._live:
                q.put_nowait(("event", wire))
        finally:
            q.put_nowait(("end", None))

    async def events(self) -> AsyncIterator[_Event]:
        q: asyncio.Queue = asyncio.Queue()
        pump = asyncio.create_task(self._pump(q), name="remote-session-view-pump")
        toucher = asyncio.create_task(self._touch_loop(q), name="remote-session-view-touch")
        try:
            while True:
                kind, wire = await q.get()
                if kind == "end":
                    return
                if kind == "reset":
                    yield _Event(ev.ErrorEvent(RESET_CODE, RESET_MESSAGE).to_wire())
                    return
                if not isinstance(wire, dict):
                    continue
                meta: Any = wire.get(_META)
                if not isinstance(meta, dict):
                    # The bus's GAP sentinel: ws._forward_live forces a resync on it,
                    # which re-attaches and re-opens this view.
                    yield _Event(dict(wire))
                    continue
                if meta.get("epoch") != self.epoch:
                    yield _Event(ev.ErrorEvent(RESET_CODE, RESET_MESSAGE).to_wire())
                    return
                seq = meta.get("seq")
                if isinstance(seq, int) and seq <= self.head:
                    continue
                yield _Event(strip(wire), getattr(wire, "src_seq", None))
        finally:
            await cancel_and_reap(toucher, phase="remote-session-view-touch", timeout=1.0)
            await cancel_and_reap(pump, phase="remote-session-view-pump", timeout=1.0)
            self.close()


async def _queue_iter(q: asyncio.Queue) -> AsyncIterator[dict]:
    while True:
        yield await q.get()


async def open_in_process(service: Any, session_id: str) -> RemoteSessionView:
    """In-process mode: subscribe to the service's local fan-out, then snapshot."""
    q = service.subscribe_local(session_id)
    try:
        snap = await service.open(session_id)
    except BaseException:
        service.unsubscribe_local(session_id, q)
        raise

    async def _touch() -> dict:
        return dict(service.touch(session_id))

    return RemoteSessionView(snap, _queue_iter(q),
                             lambda: service.unsubscribe_local(session_id, q), _touch)
