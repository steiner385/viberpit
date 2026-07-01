import json
import asyncio
import pytest
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
