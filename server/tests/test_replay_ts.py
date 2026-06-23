import json
from server.sessions import replay


def _write(tmp_path, lines):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(ln) for ln in lines))
    return str(p)


def test_replay_attaches_iso_timestamp_as_ms(tmp_path):
    path = _write(tmp_path, [
        {"type": "user", "uuid": "u1", "timestamp": "2026-06-23T12:00:00.000Z",
         "message": {"content": "hello"}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-06-23T12:00:01.500Z",
         "message": {"model": "claude", "content": [{"type": "text", "text": "hi"}]}},
    ])
    out = replay._parse(path, max_turns=20)
    by = {type(e).__name__: e for e in out}
    assert by["UserMessage"].ts == 1782216000000.0          # 2026-06-23T12:00:00Z
    assert by["TextDelta"].ts == 1782216001500.0            # +1.5s


def test_replay_missing_timestamp_is_none(tmp_path):
    path = _write(tmp_path, [
        {"type": "user", "uuid": "u1", "message": {"content": "hello"}},
    ])
    out = replay._parse(path, max_turns=20)
    um = next(e for e in out if type(e).__name__ == "UserMessage")
    assert um.ts is None
