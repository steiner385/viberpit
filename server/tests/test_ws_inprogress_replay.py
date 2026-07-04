import json
import asyncio
from server.bridge import ws as wsmod


class _FakeDriver:
    def __init__(self, active): self._active = active
    @property
    def turn_active(self): return self._active
    def current_status(self): return "in_progress" if self._active else "idle"
    def queued(self): return []


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
    sent = []
    async def send(msg): sent.append(msg)

    # Exercise the extracted replay helper directly.
    async def run():
        # Build the closure the way ws.py does: bind orchestrator + _send.
        await wsmod._run_replay_and_complete(Orch(), send, "s1", drop_incomplete=True)  # type: ignore[attr-defined]
    asyncio.run(run())

    assert calls.get("drop_incomplete_turn") is True
    batch = next(m for m in sent if m.get("type") == "batch")
    users = [e for e in batch["events"] if e.get("type") == "user"]
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
    sent = []
    async def send(msg): sent.append(msg)

    asyncio.run(
        wsmod._run_replay_and_complete(Orch(), send, "s1", drop_incomplete=True)  # type: ignore[attr-defined]
    )

    # The history window is empty (only turn trimmed), but a reset is mandatory.
    reset_seen = any(
        m.get("type") == "replayReset"
        or (m.get("type") == "batch"
            and any(e.get("type") == "replayReset" for e in m.get("events", [])))
        for m in sent
    )
    assert reset_seen, f"live attach must emit a replayReset even on empty history; got {sent}"
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
    sent = []
    async def send(msg): sent.append(msg)

    asyncio.run(
        wsmod._run_replay_and_complete(Orch(), send, "sess-A", drop_incomplete=False)  # type: ignore[attr-defined]
    )

    batch = next(m for m in sent if m.get("type") == "batch")
    for e in batch["events"]:
        assert e.get("sessionId") == "sess-A", \
            f"replay event {e.get('type')!r} missing/mismatched sessionId: {e}"
    complete = next(m for m in sent if m.get("type") == "replayComplete")
    assert complete.get("sessionId") == "sess-A", \
        f"replayComplete missing sessionId: {complete}"


def test_idle_attach_empty_transcript_does_not_reset(tmp_path):
    """Guard the fix's scope: a NON-live attach (drop_incomplete=False) with no
    transcript must NOT emit a spurious replayReset — only replayComplete."""
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")

    class Orch:
        async def transcript_path(self, sid): return str(empty)
    sent = []
    async def send(msg): sent.append(msg)

    asyncio.run(
        wsmod._run_replay_and_complete(Orch(), send, "s1", drop_incomplete=False)  # type: ignore[attr-defined]
    )

    assert not any(
        m.get("type") == "replayReset"
        or (m.get("type") == "batch"
            and any(e.get("type") == "replayReset" for e in m.get("events", [])))
        for m in sent
    ), f"idle empty attach must not reset; got {sent}"
    assert any(m.get("type") == "replayComplete" for m in sent)
