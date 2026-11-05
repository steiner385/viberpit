"""``SessionOrchestrator.reload_plugins_live`` (D-16c residuals, #3314): the per-host fan-out."""
from __future__ import annotations

import asyncio
from unittest import mock

from fastapi.testclient import TestClient

from server.hostname import self_host_name
from server.sessions import plugin_reload
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import AgentEngine, SessionRecord


class _Driver:
    def __init__(self, result=None, exc: BaseException | None = None) -> None:
        self._result, self._exc = result, exc
        self.calls: list[bool] = []

    async def reload_plugins(self, *, hold_on_cache_impact: bool = True) -> dict:
        self.calls.append(hold_on_cache_impact)
        if self._exc is not None:
            raise self._exc
        return self._result


class _NoReload:
    """A Codex-style driver: no ``reload_plugins`` at all."""


def _orch(drivers: dict[str, object]) -> SessionOrchestrator:
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    for name, drv in drivers.items():
        orch._drivers[name] = drv
        orch._records[name] = SessionRecord(
            id=name, name=name, account="a", engine=AgentEngine.CLAUDE_CODE,
            engine_session_id="c", cols=80, rows=24, cwd="/tmp")
    return orch


def test_reload_live_reports_every_live_session_on_this_host() -> None:
    applied = _Driver({"outcome": "applied", "plugins": ["p@m"], "errorCount": 0})
    busy = _Driver(exc=plugin_reload.ReloadError(plugin_reload.RELOAD_BUSY, "mid-turn"))
    orch = _orch({"a-applied": applied, "b-busy": busy, "c-codex": _NoReload()})
    orch._drivers["orphan"] = _Driver({"outcome": "applied"})  # no record: skipped silently

    rows = asyncio.run(orch.reload_plugins_live(hold_on_cache_impact=False))

    assert [(r["outcome"], r.get("failureCode")) for r in rows] == [
        ("applied", None), ("busy", "RELOAD_BUSY"), ("skipped", None)]
    assert [r["name"] for r in rows] == ["a-applied", "b-busy", "c-codex"]
    assert all(r["host"] == self_host_name() for r in rows)
    assert rows[0]["plugins"] == ["p@m"] and rows[0]["sessionId"] == "a-applied"
    assert applied.calls == [False]


def test_reload_live_isolates_an_unexpected_exception() -> None:
    orch = _orch({
        "a": _Driver({"outcome": "held", "plugins": [], "errorCount": 0,
                      "cacheImpact": {"x": 1}}),
        "b": _Driver(exc=RuntimeError("boom")),
        "c": _Driver(exc=plugin_reload.ReloadError(plugin_reload.RELOAD_REFUSED, "no")),
        "d": _Driver(exc=plugin_reload.ReloadError(plugin_reload.RELOAD_UNAVAILABLE)),
    })
    rows = {r["name"]: r for r in asyncio.run(orch.reload_plugins_live())}
    assert rows["a"]["outcome"] == "held" and rows["a"]["cacheImpact"] == {"x": 1}
    assert (rows["b"]["outcome"], rows["b"]["failureCode"]) == ("failed", "error_internal")
    assert (rows["c"]["outcome"], rows["c"]["failureCode"]) == ("failed", "RELOAD_REFUSED")
    assert rows["d"]["outcome"] == "skipped"


def test_reload_rpc_is_registered() -> None:
    from server.ipc.daemon import create_daemon_app
    from server.ipc.orchestrator_rpc import register_orchestrator_rpc

    app = create_daemon_app()
    register_orchestrator_rpc(app, _orch({"s": _NoReload()}))
    with TestClient(app) as c:
        r = c.post("/rpc/reload_plugins_live", json={"hold_on_cache_impact": True})
    assert r.json()["result"][0]["outcome"] == "skipped"
