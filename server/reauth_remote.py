"""Edge-side reauth facade: a remote proxy to the daemon's ReauthManager, plus an in-process
adapter. Mirrors server/sessions/remote.py's RemoteOrchestrator / InProcessOrchestratorAdapter."""
from __future__ import annotations

from server.ipc.client import DaemonError
from server.reauth import ReauthBusy, ReauthNoKey, ReauthState

_ERROR_TYPES = {"ReauthBusy": ReauthBusy, "ReauthNoKey": ReauthNoKey}


def _reraise(exc: DaemonError) -> None:
    cls = _ERROR_TYPES.get(exc.err_type)   # DaemonError exposes .err_type (not .type)
    if cls is ReauthBusy:
        raise ReauthBusy(None, None) from exc
    if cls is ReauthNoKey:
        raise ReauthNoKey(str(exc)) from exc
    raise exc


class RemoteReauthManager:
    """Forwards start/status/cancel to the daemon over /rpc."""

    def __init__(self, client) -> None:
        self._client = client

    async def start(self, name: str, identity: str) -> ReauthState:
        try:
            d = await self._client.call("reauth_start", name=name, identity=identity)
        except DaemonError as exc:
            _reraise(exc)
        return ReauthState.from_dict(d)  # type: ignore[return-value]

    async def status(self, name: str) -> ReauthState:
        d = await self._client.call("reauth_status", name=name)
        return ReauthState.from_dict(d)

    async def cancel(self, name: str) -> ReauthState:
        d = await self._client.call("reauth_cancel", name=name)
        return ReauthState.from_dict(d)


class InProcessReauthAdapter:
    """Wraps a real ReauthManager behind the same async facade (sync status -> awaitable)."""

    def __init__(self, manager) -> None:
        self._m = manager

    async def start(self, name: str, identity: str) -> ReauthState:
        return await self._m.start(name, identity)

    async def status(self, name: str) -> ReauthState:
        return self._m.status(name)

    async def cancel(self, name: str) -> ReauthState:
        return await self._m.cancel(name)

    # passthroughs the lifespan/sweep code calls on the edge
    def sweep_stale(self) -> None:
        self._m.sweep_stale()

    async def shutdown(self) -> None:
        await self._m.shutdown()
