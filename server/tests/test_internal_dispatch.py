"""server/sessions/internal_dispatch.py: the primary daemon's loopback caller into
its own co-located edge. Transport-level behaviour only — the endpoint's own auth
and business logic are covered by test_internal_sessions_api.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from server.sessions.selection import AllAccountsBusy
from server.sessions.internal_dispatch import (
    EdgeUnreachable, certify_harvested_via_edge, create_via_edge)


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_success_returns_session_record():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Bearer ccsys1:")
        return httpx.Response(201, json={"session": {
            "id": "s1", "name": "x", "account": "a", "engine": "claude_code",
            "engineSessionId": "e1", "cols": 80, "rows": 24, "cwd": "/tmp",
        }})

    rec = await create_via_edge(port=8787, transport=_transport(handler), name="x")
    assert rec.id == "s1"


@pytest.mark.asyncio
async def test_connection_failure_raises_edge_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(EdgeUnreachable):
        await create_via_edge(port=8787, transport=_transport(handler), name="x")


@pytest.mark.asyncio
async def test_wiring_failure_status_raises_edge_unreachable_not_domain_error():
    # 401/403/404/5xx are "this route/token is broken", never a create_session
    # refusal — the caller must fall back, not surface a misleading domain error.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "INTERNAL_ROUTE_FORBIDDEN"}})

    with pytest.raises(EdgeUnreachable):
        await create_via_edge(port=8787, transport=_transport(handler), name="x")


@pytest.mark.asyncio
async def test_domain_refusal_reconstructs_all_accounts_busy():
    reset = datetime(2026, 7, 8, 21, 0, tzinfo=timezone.utc)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": {
            "type": "AllAccountsBusy", "message": "all accounts at/above usage ceiling",
            "data": {"earliest_reset": reset.isoformat(), "kind": "at_ceiling"},
        }})

    with pytest.raises(AllAccountsBusy) as ei:
        await create_via_edge(port=8787, transport=_transport(handler), name="x")
    assert ei.value.kind == "at_ceiling"
    assert ei.value.earliest_reset == reset


@pytest.mark.asyncio
async def test_certify_posts_session_id_with_internal_token():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    await certify_harvested_via_edge(port=8787, session_id="s1",
                                     transport=_transport(handler))
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/api/internal/certify-harvested"
    assert seen[0].headers["authorization"].startswith("Bearer ccsys1:")
    assert json.loads(seen[0].content) == {"session_id": "s1"}


@pytest.mark.asyncio
async def test_certify_connection_failure_raises_edge_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(EdgeUnreachable):
        await certify_harvested_via_edge(port=8787, session_id="s1",
                                         transport=_transport(handler))


@pytest.mark.asyncio
async def test_certify_non_2xx_raises_edge_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "INTERNAL_ROUTE_FORBIDDEN"}})

    with pytest.raises(EdgeUnreachable):
        await certify_harvested_via_edge(port=8787, session_id="s1",
                                         transport=_transport(handler))
