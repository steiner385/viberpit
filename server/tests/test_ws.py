"""WebSocket bridge tests — normalized-event transport.

Uses FastAPI's TestClient (starlette synchronous WS) against an app built with
a real SessionOrchestrator whose SDK factory builds a FakeSDKClient (no real
`claude`, no quota).  The WS bridge ships normalized events as ``batch`` frames.

Assertions:
- WS without identity header is rejected
- attach sends replayComplete
- prompt streams turnStart/textDelta/turnEnd through batch frames
- a tool needing permission emits a permissionRequest; permissionResponse lets it proceed
- an inbound frame larger than 256KB is rejected with an error frame, socket stays open
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from claude_agent_sdk import TextBlock
from ccswitch.selector import Picked
from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import PerQueryScript

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
_IDENTITY_HEADERS = {"Tailscale-User-Login": "wstest@example.com"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from ccswitch.oauth import Usage
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return Usage(10.0, reset, 10.0, reset, False)


def _seed_account(name: str) -> None:
    import time as _time
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    now_ms = int(_time.time() * 1000)
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok",
        "refreshToken": f"{name}-ref",
        "expiresAt": now_ms + 9_000_000,
        "subscriptionType": "max",
    })
    shared_mcp = paths.shared_mcp_path()
    if not shared_mcp.exists():
        write_json_secure(shared_mcp, {})


def _factory(msgs, client_cls=None):
    """Return an sdk_factory building a fake client over *msgs*."""
    cls = client_cls or FakeSDKClient

    # Mirror real_sdk_factory's signature — the orchestrator's factory wrapper
    # forwards extra_env=... whenever a session has X11/OAT env, so a fake that
    # omits it raises TypeError on those sessions ("accept and ignore it").
    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *, on_pre_compact=None):
        return cls(list(msgs), on_can_use_tool=can_use_tool)

    return make


def _drain_events(
    ws,
    *,
    until_type: str | None = None,
    until: Callable[[dict], bool] | None = None,
    timeout: float = 8.0,
) -> list[dict]:
    """Receive frames and flatten batch frames into a list of event dicts.

    Stops once a frame of *until_type* is seen, or *until(frame)* returns True
    (or timeout). Pass *until* to disambiguate frames that share a ``type`` but
    differ in shape — notably the live ``StatusEvent`` (``{type:status, state}``,
    sent in a batch) vs the orchestrator's session-list ``status`` broadcast
    (``{type:status, status}``, sent top-level). Stopping on whichever ``status``
    frame happened to arrive first made ``test_attach_emits_live_status`` flake
    with ``KeyError: 'state'`` under load (issue #285).
    """
    match = until if until is not None else (lambda e: e.get("type") == until_type)
    events: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = ws.receive_text()
        except Exception:
            break
        msg = json.loads(raw)
        if msg.get("type") == "batch":
            events.extend(msg.get("events", []))
        else:
            events.append(msg)
        if any(match(e) for e in events):
            return events
    return events


def test_drain_events_until_predicate_waits_past_mismatched_status_frame():
    """Regression #285: two frames share type 'status' — the session-list broadcast
    {sessionId,status} (no 'state') and the live StatusEvent {state,count}. Stopping on
    whichever arrived first made the reattach assertion flake with KeyError: 'state'.
    The `until` predicate must skip the broadcast and wait for the state-bearing frame.
    """
    frames = [
        # session-list broadcast first — has 'status', NOT 'state'
        json.dumps({"type": "status", "sessionId": "s1", "status": "working"}),
        # live StatusEvent arrives second, inside a batch
        json.dumps({"type": "batch", "events": [
            {"type": "status", "state": "waiting_question", "count": 0}]}),
    ]

    class _ScriptedWS:
        def __init__(self, f: list[str]) -> None:
            self._f = list(f)

        def receive_text(self) -> str:
            if not self._f:
                raise RuntimeError("drained")
            return self._f.pop(0)

    events = _drain_events(
        _ScriptedWS(frames),
        until=lambda e: e.get("type") == "status" and "state" in e,
        timeout=2.0,
    )
    state_frames = [e for e in events if e.get("type") == "status" and "state" in e]
    assert state_frames and state_frames[-1]["state"] == "waiting_question"
    # The broadcast frame (no 'state') was collected but did NOT end the drain.
    assert any(
        e.get("type") == "status" and "state" not in e for e in events
    ), "the mismatched status broadcast should still be in the drained list"


def _make_orch(msgs, *, client_cls=None) -> SessionOrchestrator:
    return SessionOrchestrator(
        sdk_factory=_factory(msgs, client_cls=client_cls),
        work_dir=str(REPO_ROOT),
        poll_interval=9999.0,
        usage_fn=_fake_usage,
    )


@pytest.fixture()
def ws_env(tmp_path):
    """Yield a factory ``build(msgs, client_cls=None) -> (app, orch, client)``.

    The returned TestClient must be entered with ``with client:`` to run the
    lifespan; the helper does this and yields the entered client.
    """
    store_home = tmp_path / "store"
    store_home.mkdir()
    shared_base = tmp_path / "dot-claude"
    shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"
    cfg_dir.mkdir(parents=True)

    env_patch = patch.dict(os.environ, {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    })
    usage_patch = patch("ccswitch.accounts.usage_for", side_effect=_fake_usage)
    compose_patch = patch("server.sessions.orchestrator.compose", return_value=cfg_dir)
    select_patch = patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha"))

    created: list[TestClient] = []

    with env_patch, usage_patch, compose_patch, select_patch:
        _seed_account("alpha")

        def build(msgs, client_cls=None):
            orch = _make_orch(msgs, client_cls=client_cls)
            app = create_app(orchestrator=orch)
            client = TestClient(app, raise_server_exceptions=False)
            client.__enter__()
            created.append(client)
            return app, orch, client

        try:
            yield build
        finally:
            for c in created:
                try:
                    c.__exit__(None, None, None)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# WS without identity header is rejected
# ---------------------------------------------------------------------------

def test_ws_no_identity_rejected(ws_env):
    _app, _orch, client = ws_env([])
    rejected = False
    try:
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()
    except Exception:
        rejected = True
    assert rejected, "expected WS without identity to be rejected"


# ---------------------------------------------------------------------------
# replayComplete is sent on attach
# ---------------------------------------------------------------------------

def test_replayComplete_sent_on_attach(ws_env):
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "test-replay")
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        events = _drain_events(ws, until_type="replayComplete")
        assert any(e.get("type") == "replayComplete" for e in events), (
            f"replayComplete not in {[e.get('type') for e in events]}"
        )

    client.portal.call(orch.close_session, session_id)


def test_attach_resume_failure_reports_error_and_keeps_ws_alive(ws_env):
    """A rehydrated/resumed session whose SDK can't start (e.g. dead account token)
    must report a SESSION_START_FAILED error and KEEP the WS open — not crash it,
    which would make the client reconnect-loop forever on a blank screen."""
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "test-resume-fail")
    session_id = session.id

    async def _boom(_sid):
        raise RuntimeError("Command failed with exit code 1")
    orch.ensure_driver = _boom

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        events = _drain_events(ws, until_type="replayComplete")
        errs = [e for e in events if e.get("type") == "error"]
        assert any(e.get("code") == "SESSION_START_FAILED" for e in errs), (
            f"no SESSION_START_FAILED in {[e.get('type') for e in events]}"
        )
        assert any(e.get("type") == "replayComplete" for e in events)
        # WS must still be alive — a follow-up frame must not raise/close.
        ws.send_text(json.dumps({"type": "detach", "sessionId": session_id}))

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# A dead outbound forwarding task closes the socket (1011) so the client reconnects
# ---------------------------------------------------------------------------
# The batcher / lifecycle-forwarder / live-stream tasks are fire-and-forget. Before
# audit W2.6 an unexpected raise in one killed the socket's tail while the socket
# stayed open-looking: frozen forever, no log, no client signal. Now a failed
# forwarding task is supervised → logged loudly → the socket is closed 1011 so the
# client's reconnect logic (which replays from disk) takes over.

def test_batcher_failure_closes_socket_1011(ws_env, monkeypatch):
    """If the outbound batcher task dies unexpectedly, the socket is useless — the
    server must close it with 1011 rather than leave the client on a frozen tail."""
    from starlette.websockets import WebSocketDisconnect
    import server.bridge.ws as wsmod

    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "batcher-boom")
    session_id = session.id

    def _boom(_buf):
        raise RuntimeError("batcher boom")
    # Kill the batcher on its very first flush (the attach's replayComplete).
    monkeypatch.setattr(wsmod, "_redact_buffer", _boom)

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        with pytest.raises(WebSocketDisconnect) as ei:
            # No batch will ever flush (the flush raises); the only frame the client
            # sees is the server's 1011 close.
            for _ in range(20):
                ws.receive_text()
        assert ei.value.code == 1011

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# attach + prompt streams the turn live
# ---------------------------------------------------------------------------

def test_attach_replays_then_live(ws_env):
    msgs = [
        f.sys_init("sess-1"),
        f.stream_start(uuid="t1"),
        f.stream_text("Hello ", uuid="t1"),
        f.stream_text("there", uuid="t1"),
        f.assistant(TextBlock(text="Hello there"), uuid="t1"),
        f.result(session_id="sess-1", uuid="t1"),
    ]
    _app, orch, client = ws_env(msgs)
    session = client.portal.call(orch.create_session, "test-live")
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        # consume up to replayComplete first
        _drain_events(ws, until_type="replayComplete")

        ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "hi"}))
        events = _drain_events(ws, until_type="turnEnd", timeout=8.0)
        types = [e.get("type") for e in events]
        assert "turnStart" in types, f"no turnStart in {types}"
        assert "textDelta" in types, f"no textDelta in {types}"
        assert "turnEnd" in types, f"no turnEnd in {types}"
        text = "".join(e.get("text", "") for e in events if e.get("type") == "textDelta")
        assert "Hello there" in text, f"unexpected text: {text!r}"

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# Replay leads with replayReset so reconnect re-attaches don't duplicate content
# ---------------------------------------------------------------------------

def test_replay_batch_leads_with_reset_on_every_attach(ws_env):
    """A transcript replay must be preceded by a replayReset event in the SAME
    batch frame. attach is re-sent on every WS reconnect (client onopen), so
    without the reset a second replay re-appends a full copy of every assistant
    block into the never-recreated reducer — the duplicating-content bug."""
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "test-reset")
    session_id = session.id

    # Point replay at a real, non-empty transcript fixture so the bridge actually
    # emits a replay batch (an empty transcript sends only replayComplete).
    fixture = str(Path(__file__).parent / "fixtures" / "transcript.jsonl")
    orch.transcript_path = lambda sid: fixture

    def _first_batch_events(ws) -> list[dict]:
        """Return the events of the first `batch` frame seen (the replay batch)."""
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            msg = json.loads(ws.receive_text())
            if msg.get("type") == "batch":
                return msg.get("events", [])
        return []

    # First attach (fresh connection).
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        batch1 = _first_batch_events(ws)
        assert batch1 and batch1[0].get("type") == "replayReset", (
            f"replay batch must lead with replayReset, got {[e.get('type') for e in batch1][:3]}"
        )
        assert any(e.get("type") in ("textDelta", "toolCall") for e in batch1), (
            "expected replayed transcript content after the reset"
        )
        _drain_events(ws, until_type="replayComplete")

    # Reconnect = a brand-new socket re-attaching the same session. The replay
    # re-runs (fresh server-side live_tasks), and the reset must lead it again.
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws2:
        ws2.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        batch2 = _first_batch_events(ws2)
        assert batch2 and batch2[0].get("type") == "replayReset", (
            f"re-attach replay must lead with replayReset, got {[e.get('type') for e in batch2][:3]}"
        )

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# A re-attach on the SAME socket without an intervening detach is NOT a silent
# no-op (incident 2026-07-05, Hole 2): the navigation-return remount attaches again
# while the unmount's detach frame was lost on a closed socket, so the session is
# still in THIS socket's live_tasks. The old `if session_id in live_tasks: return`
# returned silently → the freshly-mounted empty reducer got only the live tail and a
# false "Beginning of session". The server must now tear down this socket's forwarding
# task and re-run the full replay, and the live tail must continue exactly-once (a
# single forwarding task — no duplicate from a leaked old task).
# ---------------------------------------------------------------------------

def test_same_socket_reattach_full_replays_and_tail_exactly_once(ws_env):
    msgs = [
        f.sys_init("sess-1"),
        f.stream_start(uuid="t1"),
        f.stream_text("Hello ", uuid="t1"),
        f.stream_text("there", uuid="t1"),
        f.assistant(TextBlock(text="Hello there"), uuid="t1"),
        f.result(session_id="sess-1", uuid="t1"),
    ]
    _app, orch, client = ws_env(msgs)
    session = client.portal.call(orch.create_session, "test-rebind")
    session_id = session.id
    # Non-empty transcript so the full replay emits replayReset (not just replayComplete).
    fixture = str(Path(__file__).parent / "fixtures" / "transcript.jsonl")
    orch.transcript_path = lambda sid: fixture

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")

        # SAME socket, NO detach: the re-attach must re-run the full replay.
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        ev2 = _drain_events(ws, until_type="replayComplete")
        assert any(e.get("type") == "replayReset" for e in ev2), (
            f"re-attach was a silent no-op — no replayReset in {[e.get('type') for e in ev2]}"
        )
        assert any(e.get("type") == "replayComplete" for e in ev2), (
            f"re-attach did not full-replay — no replayComplete in {[e.get('type') for e in ev2]}"
        )

        # The live tail continues exactly-once: the rebind tore down this socket's old
        # forwarding task BEFORE opening the new stream, so a single task forwards the turn.
        # A leaked pre-rebind task would double every event (both subscribers fed by the
        # driver fan-out), inflating the text / turnEnd count within the turn window.
        ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "hi"}))
        ev3 = _drain_events(ws, until_type="turnEnd", timeout=8.0)
        types3 = [e.get("type") for e in ev3]
        assert types3.count("turnEnd") == 1, f"duplicated live tail (two forwarding tasks?): {types3}"
        text = "".join(e.get("text", "") for e in ev3 if e.get("type") == "textDelta")
        assert text == "Hello there", f"tail not exactly-once: {text!r}"

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# attach re-emits the persisted exact context usage (so a reloaded idle session
# shows real remaining context, not the per-call approximation that reads 0% for
# 1M-window sessions whose model string lost its [1m] marker)
# ---------------------------------------------------------------------------

def test_attach_reemits_persisted_context_usage(ws_env):
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "test-ctx")
    session_id = session.id
    # A 1M-window opus session sitting at ~566k tokens — the exact reading the SDK
    # gave on the last turn, persisted on the record. Without re-emit, a fresh
    # attach would fall back to the model-string guess (200k → 0%).
    rec = orch._records[session_id]
    rec.context_used_tokens = 566286
    rec.context_max_tokens = 1_000_000

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        events = _drain_events(ws, until_type="replayComplete")

    cu = next((e for e in events if e.get("type") == "contextUsage"), None)
    assert cu is not None, f"attach did not re-emit contextUsage; saw {[e.get('type') for e in events]}"
    assert cu["usedTokens"] == 566286
    assert cu["maxTokens"] == 1_000_000

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# replayComplete carries totalTurns / earliestTurn for history extent
# ---------------------------------------------------------------------------

def _write_ws_transcript(tmp_path, n_turns: int) -> str:
    """Write a JSONL transcript with n_turns user+assistant turn pairs."""
    import json as _json
    p = tmp_path / "t.jsonl"
    lines = []
    for i in range(n_turns):
        lines.append({"type": "user", "uuid": f"u{i}",
                      "message": {"content": f"q{i}"}})
        lines.append({"type": "assistant", "uuid": f"a{i}",
                      "message": {"model": "claude-x",
                                  "content": [{"type": "text", "text": f"r{i}"}],
                                  "usage": {"input_tokens": 1}}})
    p.write_text("\n".join(_json.dumps(o) for o in lines))
    return str(p)


def test_replay_complete_reports_turn_extent(ws_env, tmp_path):
    """replayComplete must carry earliestTurn and totalTurns so the client knows
    whether older history exists and can offer a 'load more' affordance.

    A 25-turn transcript with the default limit=20 window delivers turns 5-24,
    so earliestTurn=5 and totalTurns=25."""
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "test-turn-extent")
    session_id = session.id

    # 25-turn transcript → default window is last 20 turns (5..24).
    transcript = _write_ws_transcript(tmp_path, 25)
    orch.transcript_path = lambda sid: transcript

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        events = _drain_events(ws, until_type="replayComplete")

    done = [e for e in events if e.get("type") == "replayComplete"]
    assert done, "replayComplete must be sent"
    assert done[-1].get("totalTurns") == 25, (
        f"expected totalTurns=25, got {done[-1]}"
    )
    assert done[-1].get("earliestTurn") == 5, (
        f"expected earliestTurn=5, got {done[-1]}"
    )

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# Permission round-trip: tool needs permission, allow lets it proceed
# ---------------------------------------------------------------------------

class _PermissionFakeClient(PerQueryScript, FakeSDKClient):
    """Fake client that invokes can_use_tool before streaming, simulating a
    tool that requires interactive approval, then proceeds once approved."""

    async def receive_response(self):
        # First message announces the turn.
        yield f.stream_start(uuid="t1")
        # Ask for permission for a Bash tool — blocks until resolve/deny.
        if self.on_can_use_tool is not None:
            await self.on_can_use_tool("Bash", {"command": "ls"}, None)
        if self.interrupted:
            return
        # Approved → finish the turn.
        yield f.stream_text("done", uuid="t1")
        yield f.result(session_id="sess-1", uuid="t1")


def test_permission_round_trip(ws_env):
    _app, orch, client = ws_env([], client_cls=_PermissionFakeClient)
    # The permission must be requested AND resolved under the same identity that
    # the WS connection presents, so create the session with the WS identity.
    session = client.portal.call(
        lambda: orch.create_session("test-perm", identity="wstest@example.com")
    )
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")

        ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "go"}))
        events = _drain_events(ws, until_type="permissionRequest", timeout=8.0)
        req = next((e for e in events if e.get("type") == "permissionRequest"), None)
        assert req is not None, f"no permissionRequest in {[e.get('type') for e in events]}"
        assert req.get("tool") == "Bash"
        perm_id = req["permId"]

        ws.send_text(json.dumps({
            "type": "permissionResponse", "sessionId": session_id,
            "permId": perm_id, "decision": "allow",
        }))

        events = _drain_events(ws, until_type="turnEnd", timeout=8.0)
        types = [e.get("type") for e in events]
        assert "turnEnd" in types, f"turn did not proceed after allow: {types}"

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# An unknown permId response and interrupt are handled safely
# ---------------------------------------------------------------------------

def test_unknown_permission_and_interrupt_safe(ws_env):
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "test-safe")
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")

        # Unknown permId — must not crash, and must NOT be a silent no-op either:
        # a decision the daemon can't apply is reported (PERMISSION_NOT_PENDING),
        # the sibling of the answerQuestion silent-drop that froze a session on
        # 2026-09-02 (see test_ws_rejected_answer.py).
        ws.send_text(json.dumps({
            "type": "permissionResponse", "sessionId": session_id,
            "permId": "does-not-exist", "decision": "allow",
        }))
        events = _drain_events(ws, until=lambda e: e.get("type") == "error", timeout=4.0)
        err = next((e for e in events if e.get("type") == "error"), None)
        assert err is not None and err.get("code") == "PERMISSION_NOT_PENDING"
        # interrupt is accepted.
        ws.send_text(json.dumps({"type": "interrupt", "sessionId": session_id}))

        # Send a benign unknown frame and confirm we get a well-formed error back.
        ws.send_text(json.dumps({"type": "bogus", "sessionId": session_id}))
        events = _drain_events(ws, until=lambda e: e.get("type") == "error", timeout=4.0)
        err = next((e for e in events if e.get("type") == "error"), None)
        assert err is not None and err.get("code") == "UNKNOWN_TYPE"

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# answerQuestion round-trip: model asks a question, user answers it
# ---------------------------------------------------------------------------

class _QuestionFakeClient(PerQueryScript, FakeSDKClient):
    """Fake client that invokes can_use_tool with AskUserQuestion before streaming,
    blocks until the user answers, then finishes the turn (echoing the chosen answer)."""

    async def receive_response(self):
        yield f.stream_start(uuid="t1")
        # Trigger an AskUserQuestion — blocks until the WS bridge answers it.
        if self.on_can_use_tool is not None:
            await self.on_can_use_tool(
                "AskUserQuestion",
                {"questions": [{"header": "Pick one", "question": "What colour?",
                                 "options": [{"label": "Red"}, {"label": "Blue"}],
                                 "multiSelect": False}]},
                None,
            )
        if self.interrupted:
            return
        yield f.stream_text("answered", uuid="t1")
        yield f.result(session_id="sess-1", uuid="t1")


def test_answer_question_round_trip(ws_env):
    """AskUserQuestion emits a question event; answerQuestion resolves it; turn completes."""
    _app, orch, client = ws_env([], client_cls=_QuestionFakeClient)
    session = client.portal.call(
        lambda: orch.create_session("test-question", identity="wstest@example.com")
    )
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")

        ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "go"}))
        # Wait for the question event from the driver.
        events = _drain_events(ws, until_type="question", timeout=8.0)
        q_event = next((e for e in events if e.get("type") == "question"), None)
        assert q_event is not None, f"no question event in {[e.get('type') for e in events]}"
        assert q_event.get("question") == "What colour?"
        assert q_event.get("header") == "Pick one"
        options = q_event.get("options", [])
        assert any(o.get("label") == "Red" for o in options)
        question_id = q_event["questionId"]

        # Send the user's answer.
        ws.send_text(json.dumps({
            "type": "answerQuestion",
            "sessionId": session_id,
            "questionId": question_id,
            "selected": ["Red"],
        }))

        # Turn should now complete.
        events = _drain_events(ws, until_type="turnEnd", timeout=8.0)
        types = [e.get("type") for e in events]
        assert "turnEnd" in types, f"turn did not complete after answer: {types}"

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# Reattach re-emits pending questions (the critical reattach gap fix)
# ---------------------------------------------------------------------------

class _QuestionBlockingFakeClient(PerQueryScript, FakeSDKClient):
    """Fake client that triggers AskUserQuestion and stays blocked until answered.

    Unlike _QuestionFakeClient it does NOT yield a result after the question — it just
    blocks on the can_use_tool future.  This lets the test detach mid-question and
    re-attach to verify that the pending question is re-emitted on the fresh attach.
    """

    async def receive_response(self):
        yield f.stream_start(uuid="t2")
        if self.on_can_use_tool is not None:
            await self.on_can_use_tool(
                "AskUserQuestion",
                {"questions": [{"header": "Reattach header", "question": "Still here?",
                                 "options": [{"label": "Yes"}, {"label": "No"}],
                                 "multiSelect": False}]},
                None,
            )
        if self.interrupted:
            return
        yield f.stream_text("done after reattach", uuid="t2")
        yield f.result(session_id="sess-2", uuid="t2")


def test_reattach_reemits_pending_question(ws_env):
    """After a detach+reattach while a question is pending, a fresh attach re-emits
    the question event so the user (and SDK callback) are never left hanging."""
    _app, orch, client = ws_env([], client_cls=_QuestionBlockingFakeClient)
    session = client.portal.call(
        lambda: orch.create_session("test-reattach-q", identity="wstest@example.com")
    )
    session_id = session.id

    saved_question_id = None

    # --- First connection: attach, prompt, see the question, then disconnect. ---
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")

        ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "go"}))
        events = _drain_events(ws, until_type="question", timeout=8.0)
        q_event = next((e for e in events if e.get("type") == "question"), None)
        assert q_event is not None, (
            f"first attach: no question event received — {[e.get('type') for e in events]}"
        )
        assert q_event.get("question") == "Still here?"
        saved_question_id = q_event["questionId"]
        # Detach (disconnect this WS) while the question is still pending.
        ws.send_text(json.dumps({"type": "detach", "sessionId": session_id}))

    # The question future must still be pending in the registry (detach does NOT deny).
    pending = orch.pending_questions(session_id)
    assert len(pending) == 1, (
        f"question must remain pending after detach, got {pending}"
    )
    assert pending[0]["question_id"] == saved_question_id

    # --- Second connection: reattach — the pending question must be re-emitted. ---
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        # The question re-emit follows replayComplete on the SAME outbound FIFO,
        # so both frequently share one batch — drain once, to the question itself
        # (a second drain would block on an already-consumed frame).
        all_events: list[dict] = _drain_events(ws, until_type="question", timeout=8.0)

        q_reemit = next((e for e in all_events if e.get("type") == "question"), None)
        assert q_reemit is not None, (
            f"reattach: pending question was NOT re-emitted — "
            f"{[e.get('type') for e in all_events]}"
        )
        assert q_reemit.get("questionId") == saved_question_id, (
            "re-emitted question must carry the same questionId as the original"
        )
        assert q_reemit.get("question") == "Still here?"
        assert q_reemit.get("header") == "Reattach header"
        options = q_reemit.get("options", [])
        assert any(o.get("label") == "Yes" for o in options)

        # Now answer the question — the driver callback should unblock and the turn complete.
        ws.send_text(json.dumps({
            "type": "answerQuestion",
            "sessionId": session_id,
            "questionId": saved_question_id,
            "selected": ["Yes"],
        }))
        events = _drain_events(ws, until_type="turnEnd", timeout=8.0)
        types = [e.get("type") for e in events]
        assert "turnEnd" in types, (
            f"turn did not complete after answering the re-emitted question: {types}"
        )

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# Reattach re-emits a pending PLAN permission with plan=True (regression: the
# re-emit path hardcoded plan=False, so a reattach mid-plan showed the generic
# Yes/No card instead of Approve & run / Keep planning).
# ---------------------------------------------------------------------------

class _PlanPermissionBlockingFakeClient(PerQueryScript, FakeSDKClient):
    """Fake client that requests an ExitPlanMode permission and stays blocked until
    resolved, so the test can detach mid-permission and reattach to verify the
    re-emitted permissionRequest carries plan=True."""

    async def receive_response(self):
        yield f.stream_start(uuid="t3")
        if self.on_can_use_tool is not None:
            await self.on_can_use_tool("ExitPlanMode", {"plan": "ship it"}, None)
        if self.interrupted:
            return
        yield f.stream_text("done after plan approval", uuid="t3")
        yield f.result(session_id="sess-3", uuid="t3")


def test_reattach_reemits_pending_plan_permission_with_plan_flag(ws_env):
    """A pending ExitPlanMode permission must be re-emitted on reattach with plan=True
    so the UI keeps the plan-approval card (Approve & run / Keep planning)."""
    _app, orch, client = ws_env([], client_cls=_PlanPermissionBlockingFakeClient)
    session = client.portal.call(
        lambda: orch.create_session("test-reattach-plan", identity="wstest@example.com")
    )
    session_id = session.id

    # --- First connection: see the live plan permission, then detach. ---
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")

        ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "go"}))
        events = _drain_events(ws, until_type="permissionRequest", timeout=8.0)
        req = next((e for e in events if e.get("type") == "permissionRequest"), None)
        assert req is not None, f"no permissionRequest in {[e.get('type') for e in events]}"
        assert req.get("tool") == "ExitPlanMode"
        assert req.get("plan") is True, "live ExitPlanMode permission must set plan=True"
        saved_perm_id = req["permId"]
        ws.send_text(json.dumps({"type": "detach", "sessionId": session_id}))

    # --- Second connection: reattach — the pending permission must be re-emitted
    #     and must still carry plan=True. ---
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        # Same single-drain rationale as the question re-emit above: the
        # permissionRequest re-emit rides the FIFO right behind replayComplete.
        all_events: list[dict] = _drain_events(ws, until_type="permissionRequest", timeout=8.0)

        reemit = next((e for e in all_events if e.get("type") == "permissionRequest"), None)
        assert reemit is not None, (
            f"reattach: pending permission was NOT re-emitted — "
            f"{[e.get('type') for e in all_events]}"
        )
        assert reemit.get("permId") == saved_perm_id
        assert reemit.get("tool") == "ExitPlanMode"
        assert reemit.get("plan") is True, (
            "re-emitted ExitPlanMode permission must carry plan=True (was hardcoded False)"
        )

        # Resolve so the driver unblocks and the turn completes cleanly.
        ws.send_text(json.dumps({
            "type": "permissionResponse", "sessionId": session_id,
            "permId": saved_perm_id, "decision": "allow",
        }))
        events = _drain_events(ws, until_type="turnEnd", timeout=8.0)
        assert "turnEnd" in [e.get("type") for e in events]

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# Reattach emits the current live status (regression: stale 'Ready' mid-turn)
# ---------------------------------------------------------------------------

def test_attach_emits_live_status(ws_env):
    """Attaching to a session whose driver is blocked mid-turn emits a live status
    frame so the in-session header reflects the true state (regression: stale 'Ready')."""
    _app, orch, client = ws_env([], client_cls=_QuestionBlockingFakeClient)
    session = client.portal.call(
        lambda: orch.create_session("test-attach-status", identity="wstest@example.com")
    )
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")
        # Drive the turn until it blocks on the question.
        ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "go"}))
        _drain_events(ws, until_type="question", timeout=8.0)
        ws.send_text(json.dumps({"type": "detach", "sessionId": session_id}))

    # Reattach — a status frame must now be emitted, carrying the blocked state.
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        # Wait for the live StatusEvent specifically — the frame carrying `state` —
        # not the session-list `status` broadcast ({type:status, status}) that may
        # interleave ahead of it. Keying on `state` is what makes this deterministic
        # rather than racing on whichever `status` frame lands first (issue #285).
        events = _drain_events(
            ws,
            until=lambda e: e.get("type") == "status" and "state" in e,
            timeout=6.0,
        )
        status_evs = [e for e in events if e.get("type") == "status" and "state" in e]
        assert status_evs, f"no live status frame on attach — {[e.get('type') for e in events]}"
        assert status_evs[-1]["state"] in ("waiting_question", "waiting_input"), (
            f"expected a needs-you state, got {status_evs[-1]['state']}"
        )


# ---------------------------------------------------------------------------
# Reattach re-emits the live queue (regression: queued chips vanished on nav)
# ---------------------------------------------------------------------------

def test_reattach_reemits_queue(ws_env):
    """A client attaching to a live session with a queued prompt receives a
    queueUpdate frame carrying the queued item (regression: previously only the
    status count was re-emitted, so queued chips vanished on navigation)."""
    class BusySDK(PerQueryScript, FakeSDKClient):
        """Fake client whose turn never completes, so a second prompt queues."""
        async def receive_response(self):
            yield f.stream_start(uuid="t1")
            await asyncio.sleep(30)   # keep the turn busy; test ends first

    _app, orch, client = ws_env([], client_cls=BusySDK)
    session = client.portal.call(orch.create_session, "q-reattach")
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws1:
        ws1.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws1, until_type="replayComplete")
        ws1.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "first"}))
        # Wait until the turn is actually running, then queue a second prompt.
        _drain_events(ws1, until_type="status", timeout=8.0)
        ws1.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "second"}))
        _drain_events(ws1, until_type="queueUpdate", timeout=8.0)

        # Fresh attach: must be told about the queued "second" prompt.
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws2:
            ws2.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
            events = _drain_events(ws2, until_type="queueUpdate", timeout=8.0)

    qu = [e for e in events if e.get("type") == "queueUpdate"]
    assert qu, f"no queueUpdate on reattach; saw {[e.get('type') for e in events]}"
    assert qu[-1]["items"] == [{"text": "second", "images": []}]

    client.portal.call(orch.close_session, session_id)


def test_attach_reemits_empty_queue(ws_env):
    """Attach re-emits the queue even when it is EMPTY. A client that missed the
    live QueueUpdate([]) when the queue drained (WS gap mid-dequeue) otherwise
    renders its stale queued chip forever — nothing else ever clears it."""
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "q-empty")
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        events = _drain_events(ws, until_type="queueUpdate", timeout=8.0)

    qu = [e for e in events if e.get("type") == "queueUpdate"]
    assert qu, f"no queueUpdate on attach with empty queue; saw {[e.get('type') for e in events]}"
    assert qu[-1]["items"] == []

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# Frame larger than 256KB is rejected, socket stays open
# ---------------------------------------------------------------------------

def test_frame_too_large_rejected(ws_env):
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "test-big")
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")

        big_text = "x" * 300_000
        ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": big_text}))

        events = _drain_events(ws, until_type="error", timeout=4.0)
        err = next((e for e in events if e.get("type") == "error"), None)
        assert err is not None and err.get("code") == "frame_too_large", (
            f"expected frame_too_large error, got {[e.get('type') for e in events]}"
        )

        # Socket still open: a normal unknown frame still elicits a response.
        ws.send_text(json.dumps({"type": "bogus", "sessionId": session_id}))
        events = _drain_events(ws, until_type="error", timeout=4.0)
        assert any(e.get("code") == "UNKNOWN_TYPE" for e in events), (
            "socket appears closed after frame_too_large"
        )


# ---------------------------------------------------------------------------
# Detached-session attach is read-only (no resume); resume is deferred to send
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _rehydrated_detached_client(tmp_path, msgs, *, claude_session_id="resume-me"):
    """Build an app rehydrated from a seeded DETACHED session row.

    Yields ``(orch, client, session_id)`` with the patches and lifespan active.
    A rehydrated row starts ``detached=True`` with no live driver — exactly the
    state that used to trigger a blocking resume-on-attach.
    """
    import asyncio as _asyncio
    from server.db import init_db as _init_db, db_session as _db_session
    from server.db.models import Session as _DbSession

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    session_id = "rehydrated-ws-s1"
    cwd = str(tmp_path)

    async def _seed():
        await _init_db(db_url=db_url)
        async with _db_session() as db:
            db.add(_DbSession(
                id=session_id, name="rehydrated-ws-session",
                account_name="alpha", claude_session_id=claude_session_id,
                status="idle", cwd=cwd,
            ))
    _asyncio.run(_seed())

    store_home = tmp_path / "store"
    store_home.mkdir()
    shared_base = tmp_path / "dot-claude"
    shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"
    cfg_dir.mkdir(parents=True)

    env = {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "CCSWITCH_DB_URL": db_url,
    }
    with patch.dict(os.environ, env), \
         patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
         patch("server.sessions.orchestrator.configdir"), \
         patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")), \
         patch("ccswitch.accounts.usage_for", side_effect=_fake_usage):
        # Seed INSIDE the CCSWITCH_HOME patch so paths.slot_path() resolves to the
        # temp store — otherwise it leaks an "alpha" account into the real store
        # (the conftest guard now fails the test if this regresses).
        _seed_account("alpha")
        orch = _make_orch(msgs)
        app = create_app(orchestrator=orch)
        with TestClient(app, raise_server_exceptions=False) as client:
            yield orch, client, session_id


def test_attach_detached_session_replays_without_resuming(tmp_path):
    """Attaching to a detached session must replay history + send replayComplete
    WITHOUT resuming a live driver. Resuming on mere view is what spawned an SDK
    subprocess + MCP fleet and hung the load forever behind the ensure-lock."""
    with _rehydrated_detached_client(tmp_path, []) as (orch, client, session_id):
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
            events = _drain_events(ws, until_type="replayComplete", timeout=8.0)
            assert any(e.get("type") == "replayComplete" for e in events), (
                f"replayComplete not in {[e.get('type') for e in events]}"
            )
        # No driver was spun up, and the record stays detached — read-only view.
        assert orch.driver(session_id) is None, (
            "attach must NOT resume a driver for a detached session"
        )
        rec = orch.get_session(session_id)
        assert rec is not None and rec.detached is True, (
            "a read-only attach must leave the session detached"
        )


def test_prompt_resumes_detached_session_and_streams(tmp_path):
    """Sending a prompt to a read-only (detached) session resumes the driver lazily
    and streams the turn — the deferred resume happens on user action, not on view."""
    msgs = [
        f.sys_init("sess-1"),
        f.stream_start(uuid="t1"),
        f.stream_text("Hi", uuid="t1"),
        f.assistant(TextBlock(text="Hi"), uuid="t1"),
        f.result(session_id="sess-1", uuid="t1"),
    ]
    with _rehydrated_detached_client(tmp_path, msgs) as (orch, client, session_id):
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
            _drain_events(ws, until_type="replayComplete", timeout=8.0)
            assert orch.driver(session_id) is None, "still not resumed after read-only attach"

            ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "go"}))
            events = _drain_events(ws, until_type="turnEnd", timeout=8.0)
            types = [e.get("type") for e in events]
            assert "turnStart" in types, f"no turnStart in {types}"
            assert "textDelta" in types, f"no textDelta in {types}"
            assert "turnEnd" in types, f"no turnEnd in {types}"
        assert orch.driver(session_id) is not None, "prompt must lazily resume the driver"


def test_prompt_resume_timeout_emits_error_and_keeps_ws_alive(tmp_path):
    """A resume that hangs (saturated box / stuck MCP fleet) must surface a
    SESSION_RESUME_TIMEOUT error frame within the timeout instead of hanging the
    socket forever, and the WS must stay alive."""
    import server.bridge.ws as wsmod

    with _rehydrated_detached_client(tmp_path, []) as (orch, client, session_id):
        async def _hang(*_a, **_k):
            await asyncio.sleep(30)

        orch.ensure_driver = _hang

        with patch.object(wsmod, "_RESUME_TIMEOUT", 0.3):
            with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
                _drain_events(ws, until_type="replayComplete", timeout=8.0)

                ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "go"}))
                events = _drain_events(ws, until_type="error", timeout=4.0)
                assert any(e.get("code") == "SESSION_RESUME_TIMEOUT" for e in events), (
                    f"no SESSION_RESUME_TIMEOUT in {[e.get('type') for e in events]}"
                )
                # Socket still alive — an unknown frame still elicits a response.
                ws.send_text(json.dumps({"type": "bogus", "sessionId": session_id}))
                evs = _drain_events(ws, until_type="error", timeout=4.0)
                assert any(e.get("code") == "UNKNOWN_TYPE" for e in evs), (
                    "socket appears closed after a resume timeout"
                )


def test_daemon_timeout_on_resume_takes_the_timeout_branch_not_the_1013_close(tmp_path):
    """Ordering guard (issue #734).

    In remote mode a resume that outruns _RESUME_TIMEOUT arrives as DaemonTimeout, which
    SUBCLASSES DaemonUnavailable. ws.py's `except (asyncio.TimeoutError, DaemonTimeout)`
    only wins because it sits BEFORE `except DaemonUnavailable`; swap the two and a slow
    resume silently regresses to websocket.close(1013) — the reconnect-loop-on-a-blank-
    screen behaviour this change removed. Type alone does not pin that, so pin the
    outcome: the retry frame must be emitted AND the socket must survive.
    """
    from server.ipc.client import DaemonTimeout

    with _rehydrated_detached_client(tmp_path, []) as (orch, client, session_id):
        async def _timed_out(*_a, **_k):
            raise DaemonTimeout("ensure_driver timed out: mock")

        orch.ensure_driver = _timed_out

        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
            _drain_events(ws, until_type="replayComplete", timeout=8.0)

            ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "go"}))
            events = _drain_events(ws, until_type="error", timeout=4.0)
            assert any(e.get("code") == "SESSION_RESUME_TIMEOUT" for e in events), (
                "a DaemonTimeout took the generic daemon-unavailable branch — check that "
                f"the except clauses are still ordered timeout-first; got {events}"
            )
            # A 1013 close would have killed the socket; it must still answer.
            ws.send_text(json.dumps({"type": "bogus", "sessionId": session_id}))
            evs = _drain_events(ws, until_type="error", timeout=4.0)
            assert any(e.get("code") == "UNKNOWN_TYPE" for e in evs), (
                "socket was closed — the 1013 branch caught the DaemonTimeout"
            )



# ---------------------------------------------------------------------------
# Live-forwarder: a crash mid-stream still reaches the UI as an error frame
# ---------------------------------------------------------------------------

def test_forward_live_emits_error_frame_on_crash():
    """If the driver's live event stream raises, the forwarder must enqueue an
    error wire frame — not log-and-swallow, which leaves the UI silent."""
    import asyncio
    from server.bridge.ws import _forward_live

    class _BoomDriver:
        async def events(self):
            raise RuntimeError("pty closed unexpectedly")
            yield  # pragma: no cover — makes this an async generator

    async def run():
        out_q: asyncio.Queue = asyncio.Queue()
        await _forward_live(_BoomDriver(), out_q, "sx")
        frames = []
        while not out_q.empty():
            frames.append(out_q.get_nowait())
        return frames

    frames = asyncio.run(run())
    errs = [fr for fr in frames if fr.get("type") == "error"]
    assert errs, f"expected an error frame, got {[fr.get('type') for fr in frames]}"
    assert "RuntimeError" in errs[0]["message"]
    assert "pty closed unexpectedly" in errs[0]["message"]


# ---------------------------------------------------------------------------
# A prompt sent during a deploy drain gets a promptRejected ack
# ---------------------------------------------------------------------------

def test_prompt_during_drain_returns_promptRejected(ws_env):
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "test-drain")
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")

        # Enter the deploy drain, then send a prompt — it must be refused AND acked.
        client.portal.call(orch.begin_drain)
        ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "held please"}))

        events = _drain_events(ws, until_type="promptRejected", timeout=8.0)
        rejected = [e for e in events if e.get("type") == "promptRejected"]
        assert rejected, f"no promptRejected in {[e.get('type') for e in events]}"
        assert rejected[0]["sessionId"] == session_id
        assert rejected[0]["text"] == "held please"
        assert rejected[0]["reason"] == "draining"
        assert rejected[0]["attachmentIds"] is None

    # No cancel_drain needed: the fixture tears down the app/loop on exit, which
    # cancels the in-memory drain + its self-clear timer task.
    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# activity message stamps last_user_interaction_at
# ---------------------------------------------------------------------------

def test_activity_message_stamps_last_user_interaction_at(ws_env):
    """Sending {"type":"activity","sessionId":...} must stamp last_user_interaction_at
    on the session record (the human-deference cooldown window is activated)."""
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "test-activity")
    session_id = session.id

    # confirm it starts None
    assert orch._records[session_id].last_user_interaction_at is None

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")

        ws.send_text(json.dumps({"type": "activity", "sessionId": session_id}))
        # Give the server a moment to process (TestClient / anyio portal is sync-driven;
        # a tiny receive_text with a short timeout lets the event loop turn).
        import time
        time.sleep(0.1)

    assert orch._records[session_id].last_user_interaction_at is not None, (
        "activity frame must stamp last_user_interaction_at on the session record"
    )
    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# prompt + interrupt:true — "interrupt and post" instead of queueing
# ---------------------------------------------------------------------------

def test_prompt_with_interrupt_flag_stops_turn_and_sends(ws_env):
    """A ``prompt`` frame carrying ``interrupt: true`` while a turn is live must stop
    that turn (SDK interrupt) and run the new text as the next turn — not park it in
    the FIFO behind the running one."""
    class StuckThenQuick(FakeSDKClient):
        async def query(self, prompt: str):
            self.sent.append(prompt)
            if len(self.sent) == 1:
                self._deliver([f.stream_start(uuid="t1")])   # never closes on its own
            else:
                self._deliver([f.result(uuid=f"r{len(self.sent)}")])

    _app, orch, client = ws_env([], client_cls=StuckThenQuick)
    session = client.portal.call(orch.create_session, "ias-ws")
    session_id = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        _drain_events(ws, until_type="replayComplete")
        ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id, "text": "first"}))
        _drain_events(ws, until=lambda e: e.get("type") == "status" and "state" in e, timeout=8.0)
        ws.send_text(json.dumps({"type": "prompt", "sessionId": session_id,
                                 "text": "now", "interrupt": True}))
        # The new prompt's echo proves it became a turn of its own.
        events = _drain_events(
            ws, until=lambda e: e.get("type") == "user" and e.get("text") == "now",
            timeout=8.0)

    sdk = orch._drivers[session_id]._client
    assert sdk.interrupted is True, "interrupt:true must stop the live turn"
    assert sdk.sent == ["first", "now"], sdk.sent
    assert any(e.get("type") == "user" and e.get("text") == "now" for e in events), \
        [e.get("type") for e in events]

    client.portal.call(orch.close_session, session_id)
