"""A session's initial prompt must survive a launch-time model approval (2026-09-02).

Live incident: dispatching a session with model=fable parks it ``pending_approval``.
The user opens it — the WS attach takes the READ-ONLY detached path (no driver
yet) — and clicks Approve. ``_spawn_session`` then deferred the initial prompt
"until the first WS attach"… which had already happened, before the spawn, on the
detached path that never pops. Nothing re-attached, so the prompt was orphaned:
the session sat idle, whatever the user typed next ("continue") ran instead, and
the fallback titler named the session after the CLI's ``/model`` bookkeeping
("Set model to Fable").

The fix that closed the incident lived in the bridge (send the parked prompt
whenever a live stream opens). The contract is now stronger: ``_spawn_session``
SENDS the initial prompt itself, so the approval alone starts the turn — no
attach, re-attach or typed prompt is needed, and the order "original prompt
first, then whatever the user types" holds by construction.
"""
from __future__ import annotations

import json
import time

from server.tests.fakes import fake_sdk as f
from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events
from server.tests.test_ws import ws_env as ws_env  # noqa: F401 — fixture lives in test_ws, not conftest


def _hold(ws_env):
    """Create a gated (fable) session; return (orch, client, session_id)."""
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


def test_launch_approval_alone_sends_the_initial_prompt(ws_env):
    """Approve with NO socket open at all: the spawn sends the prompt. This is the
    property the incident lacked — delivery must not depend on anyone watching."""
    orch, client, sid = _hold(ws_env)
    client.portal.call(
        lambda: orch.resolve_model_approval(sid, "approve", identity="wstest@example.com"))
    driver = orch.driver(sid)
    assert driver is not None
    assert driver._client.sent == ["Hello!"], (
        f"the initial prompt was orphaned by the approval; SDK saw {driver._client.sent!r}")


def test_prompt_typed_after_launch_approval_runs_after_the_initial_prompt(ws_env):
    """The incident shape: attach while pending, approve, then TYPE something. The
    initial prompt must reach the SDK before the typed one — never be orphaned
    behind it, and never be sent twice by the attach."""
    orch, client, sid = _hold(ws_env)
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
        _drain_events(ws, until_type="replayComplete")      # detached, read-only attach
        client.portal.call(
            lambda: orch.resolve_model_approval(sid, "approve", identity="wstest@example.com"))
        driver = orch.driver(sid)
        assert driver is not None
        assert driver._client.sent == ["Hello!"], "the spawn itself sends the initial prompt"

        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "continue"}))
        _wait_sent(driver, 2)

    assert driver._client.sent == ["Hello!", "continue"], (
        f"initial prompt orphaned or duplicated around the typed prompt; "
        f"SDK saw {driver._client.sent!r}")
