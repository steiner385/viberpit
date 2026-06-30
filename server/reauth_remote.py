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
    """Forwards start/status/cancel/submit_code to the daemon over /rpc."""

    def __init__(self, client) -> None:
        self._client = client

    async def start(self, name: str, identity: str, mode: str = "login") -> ReauthState:
        try:
            d = await self._client.call("reauth_start", name=name, identity=identity, mode=mode)
        except DaemonError as exc:
            _reraise(exc)
        return ReauthState.from_dict(d)  # type: ignore[return-value]

    async def status(self, name: str, *, scoped: bool = False) -> ReauthState:
        d = await self._client.call("reauth_status", name=name, scoped=scoped)
        return ReauthState.from_dict(d)

    async def cancel(self, name: str) -> ReauthState:
        d = await self._client.call("reauth_cancel", name=name)
        return ReauthState.from_dict(d)

    async def submit_code(self, name: str, code: str) -> ReauthState:
        try:
            d = await self._client.call("reauth_submit_code", name=name, code=code)
        except DaemonError as exc:
            _reraise(exc)
        return ReauthState.from_dict(d)  # type: ignore[return-value]

    # The daemon owns the reauth lifecycle in remote mode (it runs the runner, the
    # reauth-tmp sweep, and shutdown in its own lifespan). The edge calls these in its
    # lifespan against app.state.reauth, so the proxy must accept them as no-ops.
    def sweep_stale(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


class InProcessReauthAdapter:
    """Wraps a real ReauthManager behind the same async facade (sync status -> awaitable)."""

    def __init__(self, manager) -> None:
        self._m = manager

    async def start(self, name: str, identity: str, mode: str = "login") -> ReauthState:
        return await self._m.start(name, identity, mode)

    async def status(self, name: str, *, scoped: bool = False) -> ReauthState:
        return self._m.status(name, scoped=scoped)

    async def cancel(self, name: str) -> ReauthState:
        return await self._m.cancel(name)

    async def submit_code(self, name: str, code: str) -> ReauthState:
        return await self._m.submit_code(name, code)

    # passthroughs the lifespan/sweep code calls on the edge
    def sweep_stale(self) -> None:
        self._m.sweep_stale()

    async def shutdown(self) -> None:
        await self._m.shutdown()
