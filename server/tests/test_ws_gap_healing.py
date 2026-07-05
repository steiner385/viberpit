# server/tests/test_ws_gap_healing.py
import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f


class _FakeEvent:
    def __init__(self, wire): self._wire = wire
    def to_wire(self): return dict(self._wire)


class _BoomEvent:
    def to_wire(self): raise RuntimeError("bad event")


class _FakeDriver:
    def __init__(self, events): self._events = events
    async def events(self):
        for e in self._events:
            yield e


@pytest.mark.asyncio
async def test_forward_live_stamps_seq_and_session_id():
    from server.bridge.ws import _forward_live
    out_q: asyncio.Queue = asyncio.Queue()
    driver = _FakeDriver([_FakeEvent({"type": "textDelta", "text": "a"}),
                          _FakeEvent({"type": "textDelta", "text": "b"})])
    await _forward_live(driver, out_q, "sess-1")
    first = out_q.get_nowait()
    second = out_q.get_nowait()
    assert first["sessionId"] == "sess-1" and first["seq"] == 0
    assert second["sessionId"] == "sess-1" and second["seq"] == 1


@pytest.mark.asyncio
async def test_forward_live_resyncs_on_daemon_gap():
    """A daemon replay-gap sentinel (issue #185) must NOT be forwarded to the client
    as an event — instead it consumes a seq, leaving a hole the client's liveBatch
    gap-healing detects → detach+attach → full transcript replay from disk. Without
    this the edge view silently stays behind durable storage after a ring eviction."""
    from server.bridge.ws import _forward_live
    from server.ipc import protocol as proto
    out_q: asyncio.Queue = asyncio.Queue()
    driver = _FakeDriver([
        _FakeEvent({"type": "textDelta", "text": "a"}),
        _FakeEvent({"type": proto.GAP, "stream": "sess-1", "lost": 3, "oldest": 9}),
        _FakeEvent({"type": "textDelta", "text": "c"}),
    ])
    await _forward_live(driver, out_q, "sess-1")
    got = [out_q.get_nowait(), out_q.get_nowait()]
    assert out_q.empty()  # the gap sentinel itself is never forwarded
    assert [e["text"] for e in got] == ["a", "c"]
    assert [e["seq"] for e in got] == [0, 2]  # seq 1 consumed by the gap → visible hole


@pytest.mark.asyncio
async def test_forward_live_resyncs_on_local_overflow_gap():
    """The synthetic GAP the edge client injects on a LOCAL subscriber-queue
    overflow (transcript audit V1) carries oldest=None — no daemon seq anchor.
    It must be handled by the SAME _forward_live branch as the daemon-eviction
    GAP: not forwarded, consuming a seq so the client self-heals from disk."""
    from server.bridge.ws import _forward_live
    from server.ipc import protocol as proto
    out_q: asyncio.Queue = asyncio.Queue()
    driver = _FakeDriver([
        _FakeEvent({"type": "textDelta", "text": "a"}),
        _FakeEvent({"type": proto.GAP, "stream": "sess-1", "lost": 3, "oldest": None}),
        _FakeEvent({"type": "textDelta", "text": "c"}),
    ])
    await _forward_live(driver, out_q, "sess-1")
    got = [out_q.get_nowait(), out_q.get_nowait()]
    assert out_q.empty()  # the gap sentinel itself is never forwarded
    assert [e["text"] for e in got] == ["a", "c"]
    assert [e["seq"] for e in got] == [0, 2]  # seq 1 consumed by the gap → visible hole


@pytest.mark.asyncio
async def test_forward_live_gap_on_to_wire_failure():
    """A single bad event must not kill the stream, and must leave a seq hole
    so the client self-heals."""
    from server.bridge.ws import _forward_live
    out_q: asyncio.Queue = asyncio.Queue()
    driver = _FakeDriver([_FakeEvent({"type": "textDelta", "text": "a"}),
                          _BoomEvent(),
                          _FakeEvent({"type": "textDelta", "text": "c"})])
    await _forward_live(driver, out_q, "sess-1")
    got = [out_q.get_nowait(), out_q.get_nowait()]
    assert out_q.empty()
    assert [e["seq"] for e in got] == [0, 2]  # seq 1 was consumed by the failed event


def test_redact_buffer_drops_unredactable_without_raising(monkeypatch):
    import server.bridge.ws as wsmod
    good = {"type": "textDelta", "text": "ok", "seq": 0}
    bad = {"type": "textDelta", "text": "boom", "seq": 1}

    def fake_redact(e):
        if e["text"] == "boom":
            raise RuntimeError("redact blew up")
        return e

    monkeypatch.setattr(wsmod, "redact_event", fake_redact)
    out = wsmod._redact_buffer([good, bad])
    assert out == [good]  # bad dropped, no exception, frame still flows


# ---------------------------------------------------------------------------
# Integration: detach+attach on a live session re-emits replayComplete
# ---------------------------------------------------------------------------
# The bug: a bare re-attach of an already-live session is a server no-op
# (`if session_id in live_tasks: return`) — no replay, no replayComplete,
# gap never healed.  The fix: detach first to remove from live_tasks, then
# attach re-runs the full LIVE-attach path (replayReset + replay + replayComplete).
#
# This test proves the POSITIVE case: on a SINGLE socket connection, after
# the first attach has made the session live (entered live_tasks), a
# detach+attach sequence still produces a second replayComplete — the
# heal path is not a no-op.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
_IDENTITY_HEADERS = {"Tailscale-User-Login": "wstest@example.com"}


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from ccswitch.oauth import Usage
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return Usage(10.0, reset, 10.0, reset, False)


def _seed_account(name: str, tmp_path) -> None:
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


def _drain_until(ws, until_type: str, timeout: float = 8.0) -> list[dict]:
    """Drain frames from ws until an event of until_type is seen or timeout.

    Flattens batch frames into individual event dicts.
    The TestClient's receive_text() is synchronous-blocking; we rely on the
    server sending replayComplete to naturally exit rather than using a
    deadline loop with a blocking call.
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


def test_detach_then_attach_on_live_session_emits_replay_complete(tmp_path):
    """Gap-heal integration test: detach+attach on a live session re-runs the
    full replay path and emits replayComplete.

    Flow:
    1. First attach → server calls ensure_driver, adds session to live_tasks,
       runs _replay_and_complete, emits replayComplete.  Session is now LIVE.
    2. detach → server cancels the forward task, pops session from live_tasks.
    3. attach → live_tasks is empty for this session, so _handle_attach's
       early-return guard is NOT hit; it re-runs the full LIVE-attach path
       and emits a second replayComplete.

    The assertion: the second replayComplete proves the replay path ran
    (rather than being a no-op).  A bare second attach (without the preceding
    detach) would be silently dropped by `if session_id in live_tasks: return`.
    """
    store_home = tmp_path / "store"
    store_home.mkdir()
    shared_base = tmp_path / "dot-claude"
    shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"
    cfg_dir.mkdir(parents=True)

    from ccswitch.selector import Picked

    env_patch = patch.dict(os.environ, {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    })
    usage_patch = patch("ccswitch.accounts.usage_for", side_effect=_fake_usage)
    compose_patch = patch("server.sessions.orchestrator.compose", return_value=cfg_dir)
    select_patch = patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha"))

    with env_patch, usage_patch, compose_patch, select_patch:
        _seed_account("alpha", tmp_path)

        def factory(cwd, config_dir, can_use_tool, mode, resume,
                    extra_env=None, *, on_pre_compact=None):
            return FakeSDKClient([f.sys_init("sess-1")], on_can_use_tool=can_use_tool)

        orch = SessionOrchestrator(
            sdk_factory=factory,
            work_dir=str(REPO_ROOT),
            poll_interval=9999.0,
            usage_fn=_fake_usage,
        )
        app = create_app(orchestrator=orch)
        with TestClient(app, raise_server_exceptions=False) as client:
            session = client.portal.call(orch.create_session, "gap-heal-test")
            session_id = session.id

            with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                # --- Step 1: first attach → session enters live_tasks ---
                ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
                events1 = _drain_until(ws, until_type="replayComplete", timeout=8.0)
                assert any(e.get("type") == "replayComplete" for e in events1), (
                    f"first attach must produce replayComplete; got "
                    f"{[e.get('type') for e in events1]}"
                )

                # --- Step 2+3: detach then attach → full replay path re-runs ---
                # The detach pops the session from live_tasks; the subsequent
                # attach re-enters _handle_attach past the `if in live_tasks` guard
                # and runs _replay_and_complete again.
                ws.send_text(json.dumps({"type": "detach", "sessionId": session_id}))
                ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
                events2 = _drain_until(ws, until_type="replayComplete", timeout=8.0)
                assert any(e.get("type") == "replayComplete" for e in events2), (
                    "detach+attach must re-run the full replay path and emit a "
                    "second replayComplete (proving the gap-heal is not a no-op); "
                    f"got {[e.get('type') for e in events2]}"
                )

            client.portal.call(orch.close_session, session_id)
