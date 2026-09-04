"""The ``interrupt`` flag of send_prompt must survive every hop between the WS bridge
and the driver: orchestrator → daemon RPC → RemoteOrchestrator proxy (fleet mode)."""
from __future__ import annotations

from unittest import mock

import pytest
from fastapi.testclient import TestClient

from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator


def _app_with_orch():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    register_orchestrator_rpc(app, orch)
    return app, orch


def test_rpc_send_prompt_forwards_interrupt_flag():
    app, orch = _app_with_orch()
    with mock.patch.object(orch, "send_prompt", new=mock.AsyncMock(return_value="sent")) as sp:
        with TestClient(app) as c:
            r = c.post("/rpc/send_prompt",
                       json={"session_id": "s1", "text": "now", "interrupt": True}).json()
        sp.assert_awaited_once_with("s1", "now", None, interrupt=True)
    assert r["result"] == {"status": "sent"}


def test_rpc_send_prompt_without_flag_is_a_plain_queued_send():
    """An edge that predates the flag (or a plain prompt) keeps the queued-send
    contract — the flag is never forwarded unless it was actually set."""
    app, orch = _app_with_orch()
    with mock.patch.object(orch, "send_prompt", new=mock.AsyncMock(return_value="sent")) as sp:
        with TestClient(app) as c:
            c.post("/rpc/send_prompt", json={"session_id": "s1", "text": "hi"}).json()
        sp.assert_awaited_once_with("s1", "hi", None)


class _FakeClient:
    def __init__(self):
        self.last_call = None

    async def call(self, method, **kwargs):
        self.last_call = (method, kwargs)
        return {"status": "sent"}


@pytest.mark.asyncio
async def test_remote_orchestrator_send_prompt_forwards_interrupt_flag():
    client = _FakeClient()
    ro = RemoteOrchestrator(client)
    status = await ro.send_prompt("s1", "now", None, interrupt=True)
    assert status == "sent"
    assert client.last_call == ("send_prompt",
                                {"session_id": "s1", "text": "now", "images": None,
                                 "interrupt": True})


@pytest.mark.asyncio
async def test_orchestrator_send_prompt_forwards_interrupt_to_driver():
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    driver = mock.MagicMock()
    driver.send_prompt = mock.AsyncMock()
    orch._drivers["s1"] = driver
    status = await orch.send_prompt("s1", "now", None, interrupt=True)
    assert status == "sent"
    driver.send_prompt.assert_awaited_once_with("now", [], interrupt=True)
