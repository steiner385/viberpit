"""A session's initial prompt runs at creation AND its turn is visible on attach.

Originally (issue #4) the initial prompt was deferred until the first WebSocket
client attached, so the turn would stream live in front of a watching client. That
made every human-created session sit at "Ready" until somebody navigated into it.
The prompt is now sent inside ``create_session`` — for every origin — and
visibility is guaranteed the way it always was for automation-spawned sessions: a
mid-turn attach gets the user echo and the in-flight content from the driver's
turn-buffer snapshot (``open_stream``), and a settled turn replays from the
transcript.

Scenario tested:
  create_session("name", initial_prompt="Hello!")  → prompt sent to Claude NOW
  → client attaches while the turn is still open
  → user echo + assistant response reach the client
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from claude_agent_sdk import ResultMessage, TextBlock

from ccswitch.selector import Picked
from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f


REPO_ROOT = Path(__file__).parent.parent.parent
_IDENTITY_HEADERS = {"Tailscale-User-Login": "wstest@example.com"}


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from ccswitch.quota import anthropic_snapshot
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return anthropic_snapshot(10.0, reset, 10.0, reset, False)


def _seed_account(name: str) -> None:
    import time as _time
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok", "refreshToken": f"{name}-ref",
        "expiresAt": int(_time.time() * 1000) + 9_000_000, "subscriptionType": "max",
    })
    if not paths.shared_mcp_path().exists():
        write_json_secure(paths.shared_mcp_path(), {})


def _drain_events(ws, *, until_type: str, timeout: float = 8.0) -> list[dict]:
    """Receive frames and flatten batch frames into a list of event dicts."""
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


def _holding_client_cls():
    """A FakeSDKClient that delivers the scripted turn but WITHHOLDS its
    ResultMessage until ``finish()`` — so the turn is still open when the test
    attaches, the shape a real session is in when its creator navigates into it."""

    class HoldingClient(FakeSDKClient):
        instances: list = []

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            type(self).instances.append(self)

        async def query(self, prompt: str):
            self.sent.append(prompt)
            self._deliver([m for m in self._messages if not isinstance(m, ResultMessage)])

        def finish(self) -> None:
            self._deliver([m for m in self._messages if isinstance(m, ResultMessage)])

    return HoldingClient


@pytest.fixture()
def ws_env_ip(tmp_path):
    """Fixture matching the ws_env pattern but for initial-prompt tests."""
    store_home = tmp_path / "store"; store_home.mkdir()
    shared_base = tmp_path / "dot-claude"; shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"; cfg_dir.mkdir(parents=True)

    env_patch = patch.dict(os.environ, {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    })
    compose_patch = patch("server.sessions.orchestrator.compose", return_value=cfg_dir)
    trust_patch = patch("server.sessions.orchestrator.configdir.trust_project")
    select_patch = patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha"))

    created: list[TestClient] = []

    with env_patch, compose_patch, trust_patch, select_patch:
        _seed_account("alpha")

        def build(msgs, client_cls=None):
            cls = client_cls or FakeSDKClient

            def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *, on_pre_compact=None, options_overlay=None):
                return cls(list(msgs), on_can_use_tool=can_use_tool)

            orch = SessionOrchestrator(
                sdk_factory=make,
                work_dir=str(REPO_ROOT),
                poll_interval=9999.0,
                usage_fn=_fake_usage,
            )
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


def test_initial_prompt_runs_at_creation_and_is_visible_on_attach(ws_env_ip):
    """The prompt goes to Claude inside create_session — no attach needed — and a
    client that attaches while that turn is open sees the user echo and the
    assistant's output.

    Before the fix: the prompt was parked in ``_pending_initial_prompt`` until the
    first attach, so a session nobody navigated into never started working.
    """
    msgs = [
        f.sys_init("sess-ip1"),
        f.stream_start(uuid="t1"),
        f.stream_text("Hello from initial prompt", uuid="t1"),
        f.assistant(TextBlock(text="Hello from initial prompt"), uuid="t1"),
        f.result(session_id="sess-ip1", uuid="t1"),
    ]
    cls = _holding_client_cls()
    _app, orch, client = ws_env_ip(msgs, client_cls=cls)

    session = client.portal.call(
        lambda: orch.create_session("test-initial-prompt", initial_prompt="Hello!")
    )
    session_id = session.id

    # --- The prompt was sent DURING create_session, with nobody attached ---
    driver = orch.driver(session_id)
    assert driver is not None
    assert driver._client.sent == ["Hello!"], (
        f"initial prompt was not sent at creation (driver._client.sent="
        f"{driver._client.sent!r}); the session would sit at 'Ready' until opened"
    )
    assert orch.pop_pending_initial_prompt(session_id) is None, (
        "a normal initial prompt must not be parked for the WS bridge"
    )

    # --- A client attaching mid-turn sees the echo and the streamed response ---
    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
        events = _drain_events(ws, until_type="user", timeout=10.0)
        # Let the turn settle and drain to its terminal event.
        client.portal.call(driver._client.finish)
        events += _drain_events(ws, until_type="turnEnd", timeout=10.0)

    types = [e.get("type") for e in events]
    user_events = [e for e in events if e.get("type") == "user"]
    assert any(e.get("text") == "Hello!" for e in user_events), (
        f"user echo 'Hello!' missing on attach; event types: {types}"
    )
    assert "turnStart" in types or "textDelta" in types, (
        f"assistant response events missing; got: {types}"
    )
    assert "turnEnd" in types, f"turn never settled; got: {types}"

    client.portal.call(orch.close_session, session_id)


def test_initial_prompt_sent_exactly_once_across_two_attaches(ws_env_ip):
    """Attaching — once, twice, a reconnect — must never re-send the initial
    prompt. The SDK sees it exactly once: at creation."""
    msgs = [
        f.sys_init("sess-ip2"),
        f.stream_start(uuid="u1"),
        f.stream_text("Hi from initial", uuid="u1"),
        f.assistant(TextBlock(text="Hi from initial"), uuid="u1"),
        f.result(session_id="sess-ip2", uuid="u1"),
    ]
    _app, orch, client = ws_env_ip(msgs)

    session = client.portal.call(
        lambda: orch.create_session("ip-once-test", initial_prompt="Ping!")
    )
    session_id = session.id
    driver = orch.driver(session_id)
    assert driver is not None
    assert driver._client.sent == ["Ping!"]

    for _ in range(2):
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
            _drain_events(ws, until_type="replayComplete", timeout=5.0)

    assert driver._client.sent == ["Ping!"], (
        f"initial prompt was re-sent by an attach: SDK saw {driver._client.sent!r}"
    )
    assert orch.pop_pending_initial_prompt(session_id) is None

    client.portal.call(orch.close_session, session_id)
