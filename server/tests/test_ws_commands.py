"""Tests for WS bridge: slash-command catalog emission + command dispatch.

Tests that after an 'attach' the server sends a 'commands' frame, and that
'command' messages are handled with 'commandResult' responses.

Follows the pattern in test_initial_prompt_visible.py (real SessionOrchestrator
replaced by a fake stub, TestClient websocket, _IDENTITY_HEADERS).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app import create_app

REPO_ROOT = Path(__file__).parent.parent.parent
_IDENTITY_HEADERS = {"Tailscale-User-Login": "wstest@example.com"}


# ---------------------------------------------------------------------------
# Fake orchestrator — minimal surface the WS bridge calls during attach/receive
# ---------------------------------------------------------------------------

class FakeOrch:
    """Fake orchestrator stub covering just the WS bridge's call surface."""

    def __init__(self):
        self.ran: list[tuple[str, str]] = []
        # _lifespan's shutdown branch iterates orchestrator._drivers to close
        # live sessions; without it, teardown raises AttributeError.
        self._drivers: dict = {}

    # -- lifecycle (app.py calls these in _lifespan) --
    async def rehydrate(self):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass

    # -- event subscription (ws.py uses these) --
    def subscribe_events(self):
        return asyncio.Queue()

    def unsubscribe_events(self, q):
        pass

    # -- attach path --
    async def ensure_driver(self, session_id, identity):
        # ws.py's attach path REQUIRES open_stream() (the uniform stream
        # interface — no hasattr branching), so a bare object() no longer
        # survives attach. Everything else on the driver is best-effort.
        class _Stream:
            turn_active = False
            turn_started_at = None
            async def events(self):
                if False:
                    yield None
                await asyncio.Event().wait()   # block until detach/teardown
            def close(self): pass

        class _Driver:
            def open_stream(self): return _Stream()
        return _Driver()

    def get_session(self, session_id):
        return None

    def transcript_path(self, session_id):
        return None  # no replay

    @property
    def _registry(self):
        class _Reg:
            def pending(self, sid):
                return []
        return _Reg()

    def pending_questions(self, session_id):
        return []

    def pop_pending_initial_prompt(self, session_id):
        return None

    def pop_pending_initial_attachment_ids(self, session_id):
        return None

    # -- Task 6 surface --
    async def command_catalog(self, session_id):
        return [{"name": "help", "description": "Show help", "argumentHint": None, "kind": "client"}]

    async def run_command(self, session_id, name, args):
        self.ran.append((name, args))
        if name == "model":
            return {"name": "model", "render": "notice", "data": {"text": "Model → sonnet"}}
        if name == "bogus":
            raise KeyError(name)
        # "passthrough" — returns None (no commandResult should be sent)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drain_until(ws, *, until_type: str, timeout: float = 5.0) -> list[dict]:
    """Collect frames until we see one of the given type (or timeout)."""
    frames: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = ws.receive_text()
        except Exception:
            break
        msg = json.loads(raw)
        frames.append(msg)
        if msg.get("type") == until_type:
            return frames
    return frames


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def ws_fake():
    """Build the FastAPI app wired to FakeOrch and return (orch, TestClient)."""
    orch = FakeOrch()
    app = create_app(orchestrator=orch)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield orch, client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_attach_emits_commands_frame(ws_fake):
    """After 'attach', server must send a 'commands' frame (may arrive after replayComplete)."""
    _orch, client = ws_fake

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": "s1"}))

        # Collect frames until we see the 'commands' frame (or timeout).
        all_frames: list[dict] = []
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                raw = ws.receive_text()
            except Exception:
                break
            msg = json.loads(raw)
            all_frames.append(msg)
            if msg.get("type") == "commands":
                break

        types = [f.get("type") for f in all_frames]
        assert "commands" in types, (
            f"Expected a 'commands' frame after attach; got frames: {types}"
        )

        cmd_frame = next(f for f in all_frames if f.get("type") == "commands")
        assert cmd_frame.get("sessionId") == "s1"
        assert isinstance(cmd_frame.get("commands"), list)
        assert len(cmd_frame["commands"]) >= 1
        assert cmd_frame["commands"][0]["name"] == "help"


class FakeDetachedOrch(FakeOrch):
    """A session that is DETACHED (no live driver) but has a cached catalog. The WS
    bridge must replay the cache on attach without resuming a driver."""

    def __init__(self):
        super().__init__()
        self.ensure_driver_called = False

    def get_session(self, session_id):
        from types import SimpleNamespace
        return SimpleNamespace(
            detached=True, context_used_tokens=None, context_max_tokens=None, model=None,
        )

    async def ensure_driver(self, session_id, identity):
        self.ensure_driver_called = True
        return object()


def test_detached_attach_replays_cached_commands_without_resume():
    """A detached session emits its cached command catalog on attach and does NOT
    resume a driver — so the composer palette populates on view (the regression fix)."""
    orch = FakeDetachedOrch()
    app = create_app(orchestrator=orch)
    with TestClient(app, raise_server_exceptions=False) as client:
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": "s1"}))
            frames = _drain_until(ws, until_type="commands")

    types = [f.get("type") for f in frames]
    assert "commands" in types, f"detached attach must replay cached commands; got {types}"
    cmd_frame = next(f for f in frames if f.get("type") == "commands")
    assert cmd_frame["commands"][0]["name"] == "help"
    assert orch.ensure_driver_called is False, "detached view must NOT resume a driver"


def test_command_known_returns_command_result(ws_fake):
    """A known command returns a commandResult frame with ok:true and the dispatch payload."""
    _orch, client = ws_fake

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": "s1"}))
        # Drain until commands frame (confirm attach settled).
        _drain_until(ws, until_type="commands")

        ws.send_text(json.dumps({"type": "command", "sessionId": "s1", "name": "model", "args": "sonnet"}))
        # replayComplete (and other session frames) now ride the batcher and can
        # land BETWEEN the top-level commands and commandResult frames — drain to
        # the commandResult rather than assuming frame adjacency.
        frames = _drain_until(ws, until_type="commandResult")
        frame = next((f for f in frames if f.get("type") == "commandResult"), None)

    assert frame is not None, "Expected a commandResult frame but got nothing"
    assert frame.get("type") == "commandResult"
    assert frame.get("sessionId") == "s1"
    assert frame.get("ok") is True
    assert frame.get("name") == "model"
    assert frame.get("render") == "notice"
    assert frame.get("data") == {"text": "Model → sonnet"}


def test_command_unknown_returns_ok_false(ws_fake):
    """An unknown command (raises KeyError) returns commandResult with ok:false."""
    _orch, client = ws_fake

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": "s1"}))
        _drain_until(ws, until_type="commands")

        ws.send_text(json.dumps({"type": "command", "sessionId": "s1", "name": "bogus", "args": ""}))
        frames = _drain_until(ws, until_type="commandResult")
        frame = next((f for f in frames if f.get("type") == "commandResult"), None)

    assert frame is not None, "Expected a commandResult frame but got nothing"
    assert frame.get("type") == "commandResult"
    assert frame.get("ok") is False
    assert "unknown command" in frame.get("error", "").lower()


def test_passthrough_command_sends_no_result_frame(ws_fake):
    """A passthrough command (run_command returns None) must NOT send a commandResult frame."""
    _orch, client = ws_fake

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": "s1"}))
        _drain_until(ws, until_type="commands")

        # Send a passthrough command (returns None in FakeOrch).
        ws.send_text(json.dumps({"type": "command", "sessionId": "s1", "name": "passthrough", "args": ""}))

        # Wait briefly; there must be NO commandResult frame.
        # Send a known sentinel to flush: an unknown_type error frame acts as sentinel.
        ws.send_text(json.dumps({"type": "__ping__", "sessionId": "s1"}))

        # The next frame should be the UNKNOWN_TYPE error, NOT a commandResult.
        frames: list[dict] = []
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                raw = ws.receive_text()
            except Exception:
                break
            msg = json.loads(raw)
            frames.append(msg)
            if msg.get("type") in ("error", "commandResult"):
                break

        cmd_results = [f for f in frames if f.get("type") == "commandResult"]
        assert len(cmd_results) == 0, (
            f"Expected NO commandResult for passthrough command, got: {cmd_results}"
        )
        # The error frame for the unknown __ping__ type should be present.
        errors = [f for f in frames if f.get("type") == "error"]
        assert len(errors) >= 1, f"Sentinel __ping__ error not received; frames: {frames}"
