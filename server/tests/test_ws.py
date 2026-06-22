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

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from claude_agent_sdk import TextBlock
from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f

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

    def make(cwd, config_dir, can_use_tool, mode, resume):
        return cls(list(msgs), on_can_use_tool=can_use_tool)

    return make


def _drain_events(ws, *, until_type: str, timeout: float = 8.0) -> list[dict]:
    """Receive frames and flatten batch frames into a list of event dicts.

    Stops once an event of *until_type* is seen (or timeout).
    """
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
        if any(e.get("type") == until_type for e in events):
            return events
    return events


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
    select_patch = patch("server.sessions.orchestrator.select_account", return_value="alpha")

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
# Permission round-trip: tool needs permission, allow lets it proceed
# ---------------------------------------------------------------------------

class _PermissionFakeClient(FakeSDKClient):
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

        # Unknown permId — must not crash, no error frame expected.
        ws.send_text(json.dumps({
            "type": "permissionResponse", "sessionId": session_id,
            "permId": "does-not-exist", "decision": "allow",
        }))
        # interrupt is accepted.
        ws.send_text(json.dumps({"type": "interrupt", "sessionId": session_id}))

        # Send a benign unknown frame and confirm we get a well-formed error back.
        ws.send_text(json.dumps({"type": "bogus", "sessionId": session_id}))
        events = _drain_events(ws, until_type="error", timeout=4.0)
        err = next((e for e in events if e.get("type") == "error"), None)
        assert err is not None and err.get("code") == "UNKNOWN_TYPE"

    client.portal.call(orch.close_session, session_id)


# ---------------------------------------------------------------------------
# answerQuestion round-trip: model asks a question, user answers it
# ---------------------------------------------------------------------------

class _QuestionFakeClient(FakeSDKClient):
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

class _QuestionBlockingFakeClient(FakeSDKClient):
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
        # Drain up to either replayComplete or the question re-emit (whichever comes last).
        all_events: list[dict] = []
        all_events.extend(_drain_events(ws, until_type="replayComplete"))
        # Give the bridge a moment to send the question re-emit (comes after replayComplete).
        all_events.extend(_drain_events(ws, until_type="question", timeout=4.0))

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
# Attaching to a rehydrated (detached) session lazily resumes the driver
# ---------------------------------------------------------------------------

def test_attach_rehydrated_session_gets_replay_complete(tmp_path):
    """Attaching to a rehydrated/detached session calls ensure_driver and sends replayComplete.

    Uses a file-backed DB so the app can rehydrate the row on startup.
    The session has no transcript file, so replay will be empty — but replayComplete still fires.
    """
    import asyncio as _asyncio
    from server.db import init_db as _init_db, db_session as _db_session
    from server.db.models import Session as _DbSession

    db_path = tmp_path / "ccswitch.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    session_id = "rehydrated-ws-s1"
    cwd = str(tmp_path)

    # Seed the DB row before the app starts
    async def _seed():
        await _init_db(db_url=db_url)
        async with _db_session() as db:
            db.add(_DbSession(
                id=session_id, name="rehydrated-ws-session",
                account_name="alpha", claude_session_id=None,
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

    _seed_account("alpha")

    env = {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "CCSWITCH_DB_URL": db_url,
    }
    with patch.dict(os.environ, env), \
         patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
         patch("server.sessions.orchestrator.configdir"), \
         patch("server.sessions.orchestrator.select_account", return_value="alpha"), \
         patch("ccswitch.accounts.usage_for", side_effect=_fake_usage):

        orch = _make_orch([])
        app = create_app(orchestrator=orch)
        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
                events = _drain_events(ws, until_type="replayComplete", timeout=8.0)
                assert any(e.get("type") == "replayComplete" for e in events), (
                    f"replayComplete not in {[e.get('type') for e in events]}"
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
