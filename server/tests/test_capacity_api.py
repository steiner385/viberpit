"""GET /api/capacity smoke test (spec 2026-09-19 §5)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.tests.test_pm_api import IDENTITY_HEADERS, _client
from server.tests.test_pm_api import canonical_mutation_repositories  # noqa: F401


def test_get_capacity_returns_the_ceiling_stack():
    with _client() as c:
        resp = c.get("/api/capacity", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for key in ("global", "hosts", "accounts", "runner", "products", "binding", "defects", "plan"):
            assert key in body, key


@pytest.mark.asyncio
async def test_capacity_readout_is_rpc_allowlisted_and_crosses_to_remote():
    """The production edge runs in remote mode (the sessiond endpoint env var is set), so
    ``request.app.state.sessions`` is a RemoteOrchestrator, not the raw orchestrator —
    GET /api/capacity must round-trip through the daemon RPC allowlist, not just the
    in-process facade. Same pattern as
    test_automation_overview.test_overview_crosses_rpc_without_converting_unknown_count_to_zero."""
    from fastapi import FastAPI

    from server.ipc.orchestrator_rpc import register_orchestrator_rpc
    from server.sessions.facade import InProcessOrchestratorAdapter
    from server.sessions.remote import RemoteOrchestrator

    expected = {"global": {"cap": 6, "active": 2, "free": 4}, "products": [],
                "binding": None, "defects": []}
    orch = SimpleNamespace(capacity_readout=AsyncMock(return_value=expected))
    app = FastAPI()
    app.state.rpc = {}
    app.state.evict = lambda sid: None
    register_orchestrator_rpc(app, orch)

    # The RPC method allowlist: registration must include "capacity_readout" and
    # dispatch to the orchestrator's own method (D820-style regression guard —
    # adding a seam to RemoteOrchestrator without registering it daemon-side
    # leaves production 503ing even though every local test is green).
    assert "capacity_readout" in app.state.rpc
    assert await app.state.rpc["capacity_readout"]({}) == expected

    assert await InProcessOrchestratorAdapter(orch).capacity_readout() == expected

    client = SimpleNamespace(call=AsyncMock(return_value=expected))
    remote = object.__new__(RemoteOrchestrator)
    remote._client = client
    assert await remote.capacity_readout() == expected
    client.call.assert_awaited_once_with("capacity_readout")


def _plan(binding, computed_at):
    return {"computedAt": computed_at.isoformat(), "binding": binding}


@pytest.mark.parametrize("age_s, expected", [(30, {"layer": "host", "code": "hosts_full", "text": "x"}), (7200, None)])
def test_capacity_binding_endpoint_reads_the_persisted_plan(monkeypatch, age_s, expected):
    from datetime import datetime, timedelta, timezone

    binding = {"layer": "host", "code": "hosts_full", "text": "x"}
    plan = _plan(binding, datetime.now(timezone.utc) - timedelta(seconds=age_s))
    monkeypatch.setattr("server.api.capacity.read_plan", AsyncMock(return_value=plan))
    with _client() as c:
        resp = c.get("/api/capacity/binding", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"binding": expected}


@pytest.mark.parametrize("plan", [None, "raise"])
def test_capacity_binding_endpoint_without_a_plan_is_null(monkeypatch, plan):
    read = AsyncMock(side_effect=RuntimeError("db")) if plan == "raise" else AsyncMock(return_value=plan)
    monkeypatch.setattr("server.api.capacity.read_plan", read)
    with _client() as c:
        resp = c.get("/api/capacity/binding", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"binding": None}
