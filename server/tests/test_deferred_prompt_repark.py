"""A deferred initial prompt (a parked slash command, #943) must never be
lost for good when send_prompt races a concurrent driver teardown.

Root cause (issue #1737, "send_prompt: no live driver for session=<id> — prompt
dropped"): _send_deferred_initial_prompt pops the parked prompt, then calls
orchestrator.send_prompt several awaits later (catalog emission, command dispatch
in between). A driver release that lands in that window makes send_prompt answer
"no_driver"/"recovering" — and because the prompt was already popped, it used to
be lost silently: unlike a live typed prompt (ws.prompt), there is no browser copy
of this text to re-queue.

Fix: on a non-"sent" status, ws.py re-parks the prompt via
orchestrator.repark_pending_initial_prompt so the next attach's pop retries it.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator

REPO_ROOT = Path(__file__).parent.parent.parent
_IDENTITY_HEADERS = {"Tailscale-User-Login": "wstest@example.com"}


# ---------------------------------------------------------------------------
# Orchestrator-level: repark round-trips through pop
# ---------------------------------------------------------------------------


def test_repark_makes_the_prompt_poppable_again():
    # __new__, not a real constructor: both methods under test touch only
    # _pending_initial_prompt, and a real orchestrator constructor reaches out
    # to this machine's actual accounts/config unless its home directory is
    # overridden — unnecessary and unsafe for a pure dict round-trip test.
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._pending_initial_prompt = {}
    orch._pending_initial_prompt["s1"] = "original seed"
    assert orch.pop_pending_initial_prompt("s1") == "original seed"
    assert orch.pop_pending_initial_prompt("s1") is None  # pop clears it

    orch.repark_pending_initial_prompt("s1", "original seed")
    assert orch.pop_pending_initial_prompt("s1") == "original seed"
    assert orch.pop_pending_initial_prompt("s1") is None  # popped again, gone


# ---------------------------------------------------------------------------
# WS bridge: a no_driver send re-parks instead of dropping
# ---------------------------------------------------------------------------


class FakeOrch:
    """Minimal fake covering the WS bridge's attach call surface (pattern from
    test_ws_commands.py), plus the deferred-initial-prompt delivery surface."""

    def __init__(self, *, pending_prompt: str | None, send_status: str):
        self._pending_prompt = pending_prompt
        self._send_status = send_status
        self.send_prompt_calls: list[tuple[str, str]] = []
        self.reparked: list[tuple[str, str]] = []
        self._drivers: dict = {}

    async def rehydrate(self):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass

    def subscribe_events(self):
        return asyncio.Queue()

    def unsubscribe_events(self, q):
        pass

    async def ensure_driver(self, session_id, identity):
        class _Stream:
            turn_active = False
            turn_started_at = None
            async def events(self):
                if False:
                    yield None
                await asyncio.Event().wait()
            def close(self): pass

        class _Driver:
            def open_stream(self): return _Stream()
        return _Driver()

    def get_session(self, session_id):
        return None

    def transcript_path(self, session_id):
        return None

    @property
    def _registry(self):
        class _Reg:
            def pending(self, sid):
                return []
        return _Reg()

    def pending_questions(self, session_id):
        return []

    async def command_catalog(self, session_id):
        # _emit_command_catalog only sends a 'commands' frame when this is
        # truthy (empty means "nothing to show") — an empty list here would
        # leave _drain_until(until_type="commands") waiting forever.
        return [{"name": "help", "description": "Show help", "argumentHint": None, "kind": "client"}]

    async def pop_pending_initial_prompt(self, session_id):
        # Async: ws.py always awaits this directly (every real orchestrator
        # class exposes it as async). Fires at most once, like the real dict.pop.
        prompt, self._pending_prompt = self._pending_prompt, None
        return prompt

    async def send_prompt(self, session_id, text, *a, **k):
        self.send_prompt_calls.append((session_id, text))
        return self._send_status

    async def repark_pending_initial_prompt(self, session_id, prompt):
        self.reparked.append((session_id, prompt))


def _drain_until(ws, *, until_type: str, timeout: float = 5.0) -> list[dict]:
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


def test_no_driver_reparks_the_deferred_prompt_instead_of_losing_it():
    orch = FakeOrch(pending_prompt="do the thing", send_status="no_driver")
    app = create_app(orchestrator=orch)
    with TestClient(app, raise_server_exceptions=False) as client:
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": "s1"}))
            _drain_until(ws, until_type="commands")
            # Give the best-effort deferred-prompt task a moment to run.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not orch.send_prompt_calls:
                time.sleep(0.05)

    assert orch.send_prompt_calls == [("s1", "do the thing")]
    assert orch.reparked == [("s1", "do the thing")], (
        "a no_driver send must re-park the prompt for the next attach, not drop it"
    )


def test_sent_status_does_not_repark():
    orch = FakeOrch(pending_prompt="do the thing", send_status="sent")
    app = create_app(orchestrator=orch)
    with TestClient(app, raise_server_exceptions=False) as client:
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": "s1"}))
            _drain_until(ws, until_type="commands")
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not orch.send_prompt_calls:
                time.sleep(0.05)

    assert orch.send_prompt_calls == [("s1", "do the thing")]
    assert orch.reparked == []
