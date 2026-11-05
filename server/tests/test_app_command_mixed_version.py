"""D-16a final review: an upgraded edge in front of an older daemon, and the replay /
create-fallback edges of the same rollout.

- An older daemon drops the ``command_origin`` / ``identity`` RPC keys and its
  ``send_prompt`` hands the text to the CLI — so ``/fast on`` would bypass D-14's billing
  guard and the CLI's own ``/output-style`` would write ``.claude/settings.local.json``
  into the worktree. The daemon advertises ``app_command_gate`` in ``/info``; when the
  owning daemon lacks it the edge refuses the command itself and never forwards it.
- A daemon several releases behind rejects one unknown create kwarg per call: the
  fallbacks chain.
- A re-attach replaying the daemon ring never re-shows an old ``commandResult`` notice.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest

from server.ipc import protocol as proto
from server.ipc.client import DaemonError, _SeqDict
from server.sessions.commands.catalog import app_command_refusal
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.fleet_orchestrator import _PER_SESSION
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteDriver, RemoteOrchestrator
from server.tests.test_daemon_info_engines import _info
from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events
from server.tests.test_ws import ws_env as ws_env  # noqa: F401 — fixture lives in test_ws

_OLD_INFO = {"sha": "old", "protocol": 1, "engines": {}, "epoch": "e"}
_NEW_INFO = {**_OLD_INFO, proto.CAPABILITIES_INFO_KEY: [proto.CAP_APP_COMMAND_GATE]}


class _InfoClient:
    """A DaemonClient whose ``/info`` is *info* (or raises it)."""

    def __init__(self, info):
        self.info_calls = 0
        self._info = info

    async def info(self):
        self.info_calls += 1
        if isinstance(self._info, Exception):
            raise self._info
        return self._info


# ---------------------------------------------------------------- the capability


def test_the_daemon_advertises_the_app_command_gate():
    assert proto.CAP_APP_COMMAND_GATE in _info()[proto.CAPABILITIES_INFO_KEY]


@pytest.mark.asyncio
@pytest.mark.parametrize("info,expected", [
    (_NEW_INFO, True),
    (_OLD_INFO, False),                                    # predates the key
    ({**_OLD_INFO, proto.CAPABILITIES_INFO_KEY: []}, False),
    (DaemonError("DaemonUnavailable", "down"), False),     # fail closed
])
async def test_remote_reads_the_gate_from_info(info, expected):
    assert await RemoteOrchestrator(_InfoClient(info)).has_app_command_gate("s1") is expected


@pytest.mark.asyncio
async def test_remote_caches_info_for_engines_and_the_gate():
    client = _InfoClient(_NEW_INFO)
    ro = RemoteOrchestrator(client)
    await ro.has_app_command_gate("s1")
    await ro.has_app_command_gate("s2")
    await ro._engines()
    assert client.info_calls == 1


def test_the_gate_is_asked_of_the_owning_host():
    assert "has_app_command_gate" in _PER_SESSION


def test_the_refusal_is_one_function():
    old = app_command_refusal("fast", human=True, daemon_gate=False)
    assert old is not None and old["ok"] is False and old["render"] == "notice"
    assert "older ViberPit" in old["data"]["text"]
    assert app_command_refusal("help", human=False, daemon_gate=False) is None
    assert app_command_refusal("fast", human=True) is None


# ---------------------------------------------------------------- the edge, over WS


@pytest.fixture(autouse=True)
def _a_person_at_the_composer():
    with patch("server.auth.identity.attested_front_end_hop", return_value=True):
        yield


def _pair_with(info):
    """Answer the edge's gate question as a RemoteOrchestrator over a daemon whose
    ``/info`` is *info* — the old-or-new-daemon pairing, everything else in-process."""
    remote = RemoteOrchestrator(_InfoClient(info))

    async def gate(self, session_id):
        return await remote.has_app_command_gate(session_id)

    return patch.object(InProcessOrchestratorAdapter, "has_app_command_gate", gate)


def _spy_send_prompt():
    """Record every call into the daemon's send_prompt, then run it."""
    calls: list = []
    real = SessionOrchestrator.send_prompt

    async def spy(self, session_id, text, *a, **k):
        calls.append((text, k.get("command_origin")))
        return await real(self, session_id, text, *a, **k)

    return calls, patch.object(SessionOrchestrator, "send_prompt", spy)


def _typed(ws_env, text, info):
    _app, orch, client = ws_env([])
    sid = client.portal.call(orch.create_session, "mixed").id
    calls, spy = _spy_send_prompt()
    with _pair_with(info), spy, client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
        _drain_events(ws, until_type="replayComplete")
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": text}))
        frames: list = []

        def _final_ack(f) -> bool:
            return f.get("type") == "promptAck" and f.get("stage") in ("delivered", "failed")

        for _ in range(4):
            if (any(_final_ack(f) for f in frames)
                    and any(f.get("type") == "commandResult" for f in frames)):
                break
            frames += _drain_events(ws, until=lambda f: _final_ack(f)
                                    or f.get("type") == "commandResult")
    result = next(f for f in frames if f.get("type") == "commandResult")
    ack = [f for f in frames if f.get("type") == "promptAck"][-1]["stage"]
    return result, ack, calls, orch, sid


@pytest.mark.parametrize("text", ["/fast on", "/plan on", "/output-style Concise"])
def test_an_older_daemon_never_sees_a_typed_app_command(ws_env, text):
    result, ack, calls, orch, sid = _typed(ws_env, text, _OLD_INFO)
    assert calls == []                                    # never reached send_prompt
    assert result["ok"] is False and "older ViberPit" in result["data"]["text"]
    assert ack == "failed"
    assert orch._drivers[sid].mode != "plan"


def test_a_new_daemon_runs_a_typed_app_command_as_before(ws_env):
    result, ack, calls, orch, sid = _typed(ws_env, "/plan on", _NEW_INFO)
    assert calls == [("/plan on", "composer")]
    assert result["ok"] is True and ack == "delivered"
    assert orch._drivers[sid].mode == "plan"


def _parked(ws_env, info, text="/output-style Concise"):
    """Attach to a session whose create-time prompt is a parked app command."""
    _app, orch, client = ws_env([])
    sid = client.portal.call(orch.create_session, "parked").id
    parked = [text]

    async def pop(self, session_id):
        return parked.pop() if parked else None

    calls, spy = _spy_send_prompt()
    with _pair_with(info), spy, \
            patch.object(SessionOrchestrator, "pop_pending_initial_prompt", pop), \
            client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
        frames = _drain_events(ws, until=lambda f: f.get("type") == "commandResult")
    return next(f for f in frames if f.get("type") == "commandResult"), calls


def test_an_older_daemon_never_sees_a_create_time_app_command(ws_env):
    result, calls = _parked(ws_env, _OLD_INFO)
    assert calls == []
    assert result["ok"] is False and "older ViberPit" in result["data"]["text"]


def test_a_new_daemon_takes_a_create_time_app_command_as_before(ws_env):
    result, calls = _parked(ws_env, _NEW_INFO)
    # The daemon decides on its own record (none here, so it refuses): unchanged.
    assert calls == [("/output-style Concise", "create")]
    assert result["data"]["text"] == "Slash commands from automation are not run: /output-style"


@pytest.mark.parametrize("info,ok", [(_OLD_INFO, False), (_NEW_INFO, True)])
def test_a_command_frame_follows_the_owning_daemon(ws_env, info, ok):
    _app, orch, client = ws_env([])
    sid = client.portal.call(orch.create_session, "frame").id
    with _pair_with(info), client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
        _drain_events(ws, until_type="replayComplete")
        ws.send_text(json.dumps({"type": "command", "sessionId": sid, "name": "plan",
                                 "args": "on"}))
        frames = _drain_events(ws, until=lambda f: f.get("type") == "commandResult")
    result = next(f for f in frames if f.get("type") == "commandResult")
    assert result["ok"] is ok
    assert (orch._drivers[sid].mode == "plan") is ok


# ---------------------------------------------------------------- the edge, over REST


@pytest.mark.parametrize("info,status", [(_OLD_INFO, 409), (_NEW_INFO, 200)])
def test_rest_follows_the_owning_daemon(info, status):
    from server.tests.test_output_style import _BOB, _rest
    calls, spy = _spy_send_prompt()
    with _pair_with(info), spy:
        r, driver, _rows = _rest("/plan on", headers=_BOB)
    assert r.status_code == status, r.text
    assert driver.prompts == []
    if status == 409:
        assert r.json()["detail"]["code"] == "app_command_refused"
        assert calls == [] and driver.mode == "full-auto"
    else:
        assert calls == [("/plan on", "rest")] and driver.mode == "plan"


# ---------------------------------------------------------------- chained create fallback


@pytest.mark.asyncio
async def test_create_fallbacks_chain_and_log_at_info(caplog):
    """A daemon that predates both the per-session toggles and the app-command gate
    rejects one key per call; every known-new key is shed, then the pause re-applied."""
    caplog.set_level(logging.INFO, logger="server.sessions.remote")
    calls: list = []

    class _Older:
        async def call(self, method, *, timeout=None, **kw):
            calls.append((method, kw))
            if method == "set_autopilot_paused":
                return None
            for key in ("autopilot_paused", "initial_prompt_human"):
                if key in kw:
                    raise DaemonError("TypeError", "create_session() got an unexpected "
                                      f"keyword argument '{key}'")
            return {"session": {"id": "s1", "name": "n", "account": "a",
                                "engine": "claude_code", "engineSessionId": "", "cols": 80,
                                "rows": 24, "cwd": "/tmp"}}

    rec = await RemoteOrchestrator(_Older()).create_session(  # type: ignore[arg-type]
        name="n", initial_prompt="/fast on", autopilot_paused=True,
        initial_prompt_human=True)
    assert rec.id == "s1"
    assert [m for m, _ in calls] == ["create_session"] * 3 + ["set_autopilot_paused"]
    last = calls[2][1]
    assert "autopilot_paused" not in last and "initial_prompt_human" not in last
    assert calls[3][1] == {"session_id": "s1", "paused": True}
    fallbacks = [r for r in caplog.records if "creating without" in r.getMessage()]
    assert len(fallbacks) == 2 and all(r.levelno == logging.INFO for r in fallbacks)


@pytest.mark.asyncio
async def test_an_unrelated_type_error_still_raises():
    class _Broken:
        async def call(self, method, *, timeout=None, **kw):
            raise DaemonError("TypeError", "create_session() got an unexpected keyword "
                              "argument 'initial_prompt_human'")

    # The key is not in the request, so stripping cannot help: the error stands.
    with pytest.raises(DaemonError):
        await RemoteOrchestrator(_Broken()).create_session(name="n")  # type: ignore[arg-type]


# ---------------------------------------------------------------- no replayed notices


def _seq(event: dict, seq: int) -> _SeqDict:
    return _SeqDict(event, src_seq=seq)


class _RingClient:
    """subscribe() replays the ring from *since*, then a fresh event."""

    def __init__(self, events):
        self._events = events

    def subscribe(self, session_id, since=None):
        async def gen():
            for e in self._events:
                yield e
        return gen()

    def subscribe_with_release(self, session_id, since=None):
        return self.subscribe(session_id, since), lambda: None


_NOTICE = {"type": "commandResult", "ok": True, "name": "plan", "render": "notice",
           "data": {"text": "Plan mode on"}}


@pytest.mark.asyncio
async def test_a_re_attach_does_not_replay_a_command_notice():
    ring = [_seq({"type": "userMessage", "text": "hi"}, 4), _seq(_NOTICE, 5),
            _seq({"type": "textDelta", "text": "a"}, 6),
            _seq({**_NOTICE, "name": "fast"}, 7)]           # after the attach head: live
    d = RemoteDriver(_RingClient(ring), "s1", head_seq=6, resume_since=3, turn_active=True)
    got = [(e.to_wire()["type"], e.to_wire().get("name")) async for e in d.events()]
    assert got == [("userMessage", None), ("textDelta", None), ("commandResult", "fast")]


@pytest.mark.asyncio
async def test_a_catch_up_snapshot_does_not_replay_a_command_notice():
    live = [_seq({"type": "textDelta", "text": "b"}, 8)]
    d = RemoteDriver(_RingClient(live), "s1", head_seq=7, resume_since=3)
    d.join_live()
    d.pin_catch_up([{"event": {"type": "textDelta", "text": "a"}, "seq": 6},
                    {"event": _NOTICE, "seq": 7}], head=7)
    got = [e.to_wire()["type"] async for e in d.events()]
    assert got == ["textDelta", "textDelta"]
