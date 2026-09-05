"""The ONE attach-stream implementation every agent driver hands to a subscriber.

Engine-neutral by construction: a stream is a snapshot of the in-progress turn
plus a subscriber queue, and neither concept is Claude-specific. It lives in its
own module because a second copy would drift from the settle-race fix
:class:`DriverStream` documents below — the failure it exists to prevent is
invisible in tests that attach while idle, so a divergent copy would look
correct for as long as it took a real mid-turn reattach to lose a turn.

The host driver is described structurally (:class:`_StreamHost`) rather than
imported: this module must not depend on any concrete driver, and every driver
already owns the same ``_subscribers`` fan-out set.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Final, Protocol

#: Close sentinel a driver puts on every subscriber queue in ``aclose()``.
#: Shared, because a subscriber must recognize the same object its driver sends.
_STREAM_DONE: Final = object()


class _StreamHost(Protocol):
    """The one thing a stream needs from the driver that opened it."""

    _subscribers: set[asyncio.Queue]


class DriverStream:
    """One attach's atomically-opened view of a driver's event stream.

    Built by ``<driver>.open_stream()`` with NO awaits between snapshotting
    the in-progress turn buffer, registering the subscriber queue, and capturing
    ``turn_active``/``turn_started_at`` — so all four describe the SAME instant.

    The WS attach path needs that atomicity (the settle-race bug, B3): it awaits
    a multi-step disk replay between reading ``turn_active`` and consuming the
    stream. If the turn settles during those awaits, the driver clears
    ``_turn_buffer`` — a snapshot taken *after* the replay would be empty, and
    the in-progress turn the replay dropped (``drop_incomplete=True``) would be
    delivered by NOBODY. Opening the stream first pins the snapshot and the
    flags to one consistent instant; anything emitted afterwards lands in the
    queue and is delivered live.
    """

    def __init__(self, driver: _StreamHost, snapshot: Sequence[object],
                 queue: asyncio.Queue, turn_active: bool,
                 turn_started_at: float | None) -> None:
        self._driver = driver
        self._snapshot = snapshot
        self._queue = queue
        self.turn_active = turn_active
        self.turn_started_at = turn_started_at

    async def events(self) -> AsyncIterator[object]:
        """Yield the snapshot (the past), then drain the queue (strictly the
        future) until the driver's close sentinel."""
        try:
            for e in self._snapshot:
                yield e
            while True:
                item = await self._queue.get()
                if item is _STREAM_DONE:
                    return
                yield item
        finally:
            self._driver._subscribers.discard(self._queue)

    def close(self) -> None:
        """Discard the subscriber queue without consuming ``events()``.

        Leak guard for the WS attach error path: if the replay between
        ``open_stream()`` and the live-task creation raises, nothing will ever
        iterate ``events()`` (whose ``finally`` normally deregisters), so the
        queue must be discarded here or it buffers every future emit forever.
        Idempotent — safe alongside the generator's own discard."""
        self._driver._subscribers.discard(self._queue)
