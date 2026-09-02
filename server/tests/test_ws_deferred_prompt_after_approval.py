"""A session's initial prompt must survive a launch-time model approval (2026-09-02).

Live incident: dispatching a session with model=fable parks it ``pending_approval``.
The user opens it — the WS attach takes the READ-ONLY detached path (no driver
yet) — and clicks Approve. ``_spawn_session`` then defers the initial prompt
"until the first WS attach"… which already happened, before the spawn, on the
detached path that never pops it. Nothing re-attaches, so the prompt is orphaned:
the session sits idle, whatever the user types next ("continue") runs instead,
and the fallback titler names the session after the CLI's ``/model`` bookkeeping
("Set model to Fable").

The bridge owns both ways a live stream gets opened on an already-attached
socket, so it owns the recovery: the deferred prompt is sent whenever a live
stream is opened — on a re-attach (the live path) AND on the lazy ``_ensure_live``
a typed prompt triggers — so the original prompt always runs first.
"""
from __future__ import annotations

import json
import time

from server.tests.fakes import fake_sdk as f
from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events
from server.tests.test_ws import ws_env as ws_env  # noqa: F401 — fixture lives in test_ws, not conftest


def _hold_then_attach(ws_env):
    """Create a gated (fable) session, attach BEFORE approval (detached path),
    approve, and return (orch, client, session_id, ws-context-manager)."""
    msgs = [f.sys_init("sess-fable"), f.result(session_id="sess-fable", uuid="t1")]
    _app, orch, client = ws_env(msgs)
    rec = client.portal.call(
        lambda: orch.create_session("fable-hold", initial_prompt="Hello!", model="fable"))
    assert rec.status == "pending_approval", rec.status
    return orch, client, rec.id


def _wait_sent(driver, n: int, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while len(driver._client.sent) < n and time.monotonic() < deadline:
        time.sleep(0.05)


def test_prompt_after_launch_approval_sends_the_deferred_initial_prompt_first(ws_env):
    """The incident shape: attach while pending, approve, then TYPE something. The
    deferred initial prompt must go to the SDK before the typed prompt — not be
    silently orphaned behind it."""
    orch, client, sid = _hold_then_attach(ws_env)
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
        _drain_events(ws, until_type="replayComplete")      # detached, read-only attach
        client.portal.call(
            lambda: orch.resolve_model_approval(sid, "approve", identity="wstest@example.com"))
        driver = orch.driver(sid)
        assert driver is not None
        assert driver._client.sent == [], "the initial prompt is deferred at spawn (by design)"

        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "continue"}))
        _wait_sent(driver, 2)

    assert driver._client.sent == ["Hello!", "continue"], (
        "the deferred initial prompt was orphaned by the pre-spawn attach; "
        f"SDK saw {driver._client.sent!r}")


def test_reattach_after_launch_approval_sends_the_deferred_initial_prompt(ws_env):
    """The path the session view takes once it notices the record left
    ``pending_approval``: a fresh attach frame on the same socket. The live attach
    path pops + sends the deferred prompt and its turn streams to the client."""
    orch, client, sid = _hold_then_attach(ws_env)
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
        _drain_events(ws, until_type="replayComplete")
        client.portal.call(
            lambda: orch.resolve_model_approval(sid, "approve", identity="wstest@example.com"))
        driver = orch.driver(sid)
        assert driver is not None

        ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
        events = _drain_events(ws, until_type="user", timeout=10.0)

    user_texts = [e.get("text") for e in events if e.get("type") == "user"]
    assert "Hello!" in user_texts, f"initial prompt never streamed; got {user_texts!r}"
    assert driver._client.sent == ["Hello!"]
