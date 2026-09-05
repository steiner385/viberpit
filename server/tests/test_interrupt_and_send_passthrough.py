"""The optional kwargs of send_prompt — ``interrupt`` (interrupt-and-post) and
``context`` (the assistant's route chip, spec §4.5) — must survive every hop between
the WS bridge and the driver: orchestrator → daemon RPC → RemoteOrchestrator proxy
(fleet mode).

The deployed topology makes this load-bearing rather than theoretical: the edge's
``app.state.sessions`` is a RemoteOrchestrator (or a FleetOrchestrator whose primary
is one, and whose ``_make_per_session`` forwards ``**kwargs`` verbatim), so a kwarg
that only exists on ``SessionOrchestrator`` raises TypeError on every prompt that
carries it — in production only, since in-process tests never cross the RPC hop."""
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


def test_rpc_send_prompt_forwards_context():
    """The daemon's RPC handler hands the chip to the orchestrator. Without this hop
    an assistant prompt sent from the edge reaches the driver with no context at all."""
    app, orch = _app_with_orch()
    ctx = {"route": "/pipeline", "sessionId": "abc-12345678"}
    with mock.patch.object(orch, "send_prompt", new=mock.AsyncMock(return_value="sent")) as sp:
        with TestClient(app) as c:
            c.post("/rpc/send_prompt",
                   json={"session_id": "s1", "text": "hi", "context": ctx}).json()
        sp.assert_awaited_once_with("s1", "hi", None, context=ctx)


def test_rpc_send_prompt_without_context_keeps_the_plain_contract():
    """A prompt with no chip must call the orchestrator exactly as it did before the
    chip existed — not with an extra ``context=None`` an older orchestrator would
    reject."""
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
async def test_remote_orchestrator_send_prompt_forwards_context():
    """The edge→daemon hop. `RemoteOrchestrator` is what `app.state.sessions` actually
    is in the deployed topology, so a missing kwarg here is a production-only
    TypeError on every assistant prompt that carries a chip."""
    client = _FakeClient()
    ro = RemoteOrchestrator(client)
    ctx = {"route": "/sessions/abc", "sessionId": "abc-12345678"}
    status = await ro.send_prompt("s1", "hi", None, context=ctx)
    assert status == "sent"
    assert client.last_call == ("send_prompt",
                                {"session_id": "s1", "text": "hi", "images": None,
                                 "interrupt": False, "context": ctx})


@pytest.mark.asyncio
async def test_remote_orchestrator_send_prompt_omits_context_when_unset():
    """No chip → the exact pre-chip call shape, so a newer edge talking to an older
    daemon never sends a key its RPC handler would choke on."""
    client = _FakeClient()
    ro = RemoteOrchestrator(client)
    await ro.send_prompt("s1", "hi", None)
    assert client.last_call == ("send_prompt",
                                {"session_id": "s1", "text": "hi", "images": None,
                                 "interrupt": False})


@pytest.mark.asyncio
async def test_orchestrator_send_prompt_forwards_interrupt_to_driver():
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    driver = mock.MagicMock()
    driver.send_prompt = mock.AsyncMock()
    orch._drivers["s1"] = driver
    status = await orch.send_prompt("s1", "now", None, interrupt=True)
    assert status == "sent"
    driver.send_prompt.assert_awaited_once_with("now", [], interrupt=True, context=None)


@pytest.mark.asyncio
async def test_orchestrator_send_prompt_forwards_context_to_driver():
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    driver = mock.MagicMock()
    driver.send_prompt = mock.AsyncMock()
    orch._drivers["s1"] = driver
    ctx = {"route": "/insights"}
    assert await orch.send_prompt("s1", "hi", None, context=ctx) == "sent"
    driver.send_prompt.assert_awaited_once_with("hi", [], context=ctx)
