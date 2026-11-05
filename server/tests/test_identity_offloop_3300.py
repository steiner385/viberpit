"""#3300 B: identity resolution runs off the event loop.

With no identity header ``verify_identity`` falls back to ``tailscale whois``, a
``subprocess.run`` with a 2 s timeout. Called on the loop, that froze every
connection behind it and stacked onto the model-catalog stall after a restart.
"""
from __future__ import annotations

import asyncio
import time

from starlette.requests import Request

from server.auth import middleware


def _slow_verify(result):
    def _verify(_request):
        time.sleep(0.3)  # stands in for a whois that hangs toward its timeout
        return result
    return _verify


async def _worst_tick_delay(coro) -> tuple[object, float]:
    task = asyncio.ensure_future(coro)
    worst = 0.0
    while not task.done():
        t0 = time.monotonic()
        await asyncio.sleep(0.01)
        worst = max(worst, time.monotonic() - t0 - 0.01)
    return await task, worst


def test_http_dispatch_resolves_identity_off_the_loop(monkeypatch):
    monkeypatch.setattr(middleware, "verify_identity", _slow_verify(None))
    request = Request({"type": "http", "method": "GET", "path": "/api/sessions",
                       "headers": [], "query_string": b"", "client": ("100.64.0.9", 1234)})

    async def _call_next(_request):  # pragma: no cover - 401 short-circuits
        raise AssertionError("an unverified request must not reach the route")

    mw = middleware.IdentityMiddleware(app=lambda *a: None)
    resp, worst = asyncio.run(_worst_tick_delay(mw.dispatch(request, _call_next)))
    assert resp.status_code == 401
    assert worst < 0.1, f"loop tick delayed {worst * 1000:.0f} ms by identity resolution"


def test_ws_identity_resolves_off_the_loop(monkeypatch):
    monkeypatch.setattr(middleware, "verify_identity", _slow_verify("alice@example.com"))

    class _WS:
        headers: dict[str, str] = {}

    got, worst = asyncio.run(_worst_tick_delay(middleware.require_ws_identity(_WS())))
    assert got == "alice@example.com"
    assert worst < 0.1, f"loop tick delayed {worst * 1000:.0f} ms by identity resolution"
