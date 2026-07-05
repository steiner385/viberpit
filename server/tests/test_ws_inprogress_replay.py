import json
import asyncio
from server.bridge import ws as wsmod


def _write_transcript(tmp_path):
    def turn(u, a, text):
        return [
            {"type": "user", "uuid": u, "message": {"role": "user", "content": text}},
            {"type": "assistant", "uuid": a,
             "message": {"role": "assistant", "model": "m",
                         "content": [{"type": "text", "text": text + "-r"}]}},
        ]
    rows = turn("u1", "a1", "first")
    rows += [{"type": "user", "uuid": "u2", "message": {"role": "user", "content": "second"}},
             {"type": "assistant", "uuid": "a2",
              "message": {"role": "assistant", "model": "m",
                          "content": [{"type": "text", "text": "partial"}]}}]
    p = tmp_path / "x.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return str(p)


def test_replay_trims_in_progress_turn_when_active(tmp_path, monkeypatch):
    path = _write_transcript(tmp_path)

    # Drive replay_window through the real trim, but capture what the seam requests.
    from server.sessions import replay as replaymod
    calls = {}
    real = replaymod.replay_window
    async def spy(p, **kw):
        calls.update(kw)
        return await real(p, **kw)
    monkeypatch.setattr(wsmod, "replay_window", spy)

    # Minimal orchestrator stub exposing transcript_path.
    class Orch:
        async def transcript_path(self, sid): return path
    # The helper now feeds the socket's outbound FIFO (out_q.put_nowait) rather
    # than sending a pre-built batch — frames are individual wire dicts.
    sent: list[dict] = []

    # Exercise the extracted replay helper directly.
    async def run():
        # Bind orchestrator + enqueue the way ws.py does (out_q.put_nowait).
        await wsmod._run_replay_and_complete(Orch(), sent.append, "s1", drop_incomplete=True)  # type: ignore[attr-defined]
    asyncio.run(run())

    assert calls.get("drop_incomplete_turn") is True
    users = [e for e in sent if e.get("type") == "user"]
    assert [u["text"] for u in users] == ["first"]   # "second" (in-progress) trimmed


def _write_in_progress_only_transcript(tmp_path):
    """A transcript whose ONLY content is the in-progress turn — the shape produced
    by a short/just-resumed session. drop_incomplete_turn trims this to nothing."""
    rows = [
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "only"}},
        {"type": "assistant", "uuid": "a1",
         "message": {"role": "assistant", "model": "m",
                     "content": [{"type": "text", "text": "partial"}]}},
    ]
    p = tmp_path / "y.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return str(p)


def test_live_attach_resets_even_when_trimmed_history_is_empty(tmp_path):
    """Regression (#390): a LIVE attach whose trimmed history window is empty must
    STILL send a replayReset before replayComplete. The in-progress turn is re-supplied
    by the live stream; without the reset the client renders it under a stale
    "Beginning of session" state and double-appends it on reconnect."""
    path = _write_in_progress_only_transcript(tmp_path)

    class Orch:
        async def transcript_path(self, sid): return path
    sent: list[dict] = []

    asyncio.run(
        wsmod._run_replay_and_complete(Orch(), sent.append, "s1", drop_incomplete=True)  # type: ignore[attr-defined]
    )

    # The history window is empty (only turn trimmed), but a reset is mandatory.
    assert any(m.get("type") == "replayReset" for m in sent), \
        f"live attach must emit a replayReset even on empty history; got {sent}"
    # replayComplete must still fire so the client's loading spinner clears.
    assert any(m.get("type") == "replayComplete" for m in sent)


def test_replay_frames_are_tagged_with_session_id(tmp_path):
    """Every replay frame — the replayReset, each replay content event, and the
    trailing replayComplete — must carry its owning ``sessionId``, exactly like the
    live stream stamps ``_forward_live`` events.

    Without this tag the client routes replay frames by whichever session happens to
    be subscribed when they arrive (see handleLiveBatch / useSessionRuntime.onEvent),
    not by identity. A stale replay batch for session A that lands just after the user
    navigates to B is then applied to B: the untagged replayReset wipes B's transcript
    to empty and B's history never returns (the "transcript goes blank on navigation"
    bug). Tagging lets the client drop a replay meant for a session it is no longer
    viewing."""
    path = _write_transcript(tmp_path)

    class Orch:
        async def transcript_path(self, sid): return path
    sent: list[dict] = []

    asyncio.run(
        wsmod._run_replay_and_complete(Orch(), sent.append, "sess-A", drop_incomplete=False)  # type: ignore[attr-defined]
    )

    assert sent, "expected enqueued replay frames"
    for e in sent:
        assert e.get("sessionId") == "sess-A", \
            f"replay frame {e.get('type')!r} missing/mismatched sessionId: {e}"
    complete = next(m for m in sent if m.get("type") == "replayComplete")
    assert complete.get("sessionId") == "sess-A", \
        f"replayComplete missing sessionId: {complete}"


def test_run_replay_passes_turn_started_at_to_replay_window(tmp_path, monkeypatch):
    """_run_replay_and_complete must forward turn_started_at into replay_window —
    it is the signal that lets the incomplete-turn drop distinguish the in-progress
    turn's disk user entry from a PREVIOUS turn's (resume-lag amputation bug)."""
    path = _write_transcript(tmp_path)

    from server.sessions import replay as replaymod
    calls = {}
    real = replaymod.replay_window
    async def spy(p, **kw):
        calls.update(kw)
        return await real(p, **kw)
    monkeypatch.setattr(wsmod, "replay_window", spy)

    class Orch:
        async def transcript_path(self, sid): return path
    sent: list[dict] = []

    asyncio.run(wsmod._run_replay_and_complete(  # type: ignore[attr-defined]
        Orch(), sent.append, "s1", drop_incomplete=True, turn_started_at=1751710123.5))

    assert calls.get("drop_incomplete_turn") is True
    assert calls.get("turn_started_at") == 1751710123.5


def test_attach_passes_driver_turn_started_at_to_replay_window(tmp_path, monkeypatch):
    """Full WS attach: the driver's turn_started_at (local property or the
    RemoteDriver snapshot field) must reach replay_window alongside
    drop_incomplete_turn=turn_active. Without the passthrough the drop falls back to
    the legacy positional rule and can amputate a completed turn on a resume-lag
    attach."""
    import time
    from fastapi.testclient import TestClient
    from server.app import create_app

    path = _write_transcript(tmp_path)
    started = 1751710123.5

    from server.sessions import replay as replaymod
    calls = {}
    real = replaymod.replay_window
    async def spy(p, **kw):
        calls.update(kw)
        return await real(p, **kw)
    monkeypatch.setattr(wsmod, "replay_window", spy)

    class _LiveStream:
        turn_active = True
        turn_started_at = started
        async def events(self):
            if False:
                yield None
            await asyncio.Event().wait()   # live stream: block until torn down
        def close(self): pass

    class _LiveDriver:
        identity = "wstest@example.com"
        def current_status(self): return "in_progress"
        def queued(self): return []
        def queue_wire(self): return []
        def seed_idle_suggestions(self, user_text, assistant_text): pass
        def open_stream(self):
            # ws.py reads turn_active/turn_started_at from the OPENED STREAM (the
            # atomic snapshot), not the driver — mirror the DriverStream shape.
            return _LiveStream()

    class _Reg:
        def pending(self, sid): return []

    class Orch:
        def __init__(self):
            self._drivers = {}
            self._registry = _Reg()
        async def rehydrate(self): pass
        async def start(self): pass
        async def stop(self): pass
        def subscribe_events(self): return asyncio.Queue()
        def unsubscribe_events(self, q): pass
        async def ensure_driver(self, session_id, identity): return _LiveDriver()
        async def get_session(self, session_id): return None
        async def transcript_path(self, session_id): return path
        async def command_catalog(self, session_id): return []
        def pending_questions(self, session_id): return []
        def pop_pending_initial_prompt(self, session_id): return None
        def pop_pending_initial_attachment_ids(self, session_id): return None

    app = create_app(orchestrator=Orch())
    with TestClient(app, raise_server_exceptions=False) as client:
        with client.websocket_connect(
                "/ws", headers={"Tailscale-User-Login": "wstest@example.com"}) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": "s1"}))
            # replayComplete now rides the outbound FIFO inside a batch frame —
            # flatten batches while draining (a top-level-only check would block
            # forever on receive_text once the live stream goes quiet).
            deadline = time.monotonic() + 5.0
            done = False
            while not done and time.monotonic() < deadline:
                msg = json.loads(ws.receive_text())
                events = msg.get("events", []) if msg.get("type") == "batch" else [msg]
                done = any(e.get("type") == "replayComplete" for e in events)

    assert calls.get("drop_incomplete_turn") is True
    assert calls.get("turn_started_at") == started


def test_idle_attach_empty_transcript_does_not_reset(tmp_path):
    """Guard the fix's scope: a NON-live attach (drop_incomplete=False) with no
    transcript must NOT emit a spurious replayReset — only replayComplete."""
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")

    class Orch:
        async def transcript_path(self, sid): return str(empty)
    sent: list[dict] = []

    asyncio.run(
        wsmod._run_replay_and_complete(Orch(), sent.append, "s1", drop_incomplete=False)  # type: ignore[attr-defined]
    )

    assert not any(m.get("type") == "replayReset" for m in sent), \
        f"idle empty attach must not reset; got {sent}"
    assert any(m.get("type") == "replayComplete" for m in sent)
