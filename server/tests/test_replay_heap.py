"""#2885: transcript replay must not leave the daemon's heap at the parse's high-water.

A full transcript parse runs on an asyncio.to_thread() worker; glibc keeps the freed
objects in that thread's arena, so the daemon settled at multi-GB RSS. The reader now
streams and a large parse is followed by malloc_trim(0)."""
from __future__ import annotations

import asyncio
import json
import types

from server.sessions import replay


def _write(tmp_path, n_turns=3):
    lines = []
    for i in range(n_turns):
        lines.append({"type": "user", "uuid": f"u{i}", "message": {"content": f"q{i}"}})
        lines.append({"type": "assistant", "uuid": f"a{i}",
                      "message": {"model": "claude", "content": [{"type": "text", "text": f"a{i}"}]}})
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(ln) for ln in lines) + "\n")
    return str(p)


def test_read_jsonl_streams_instead_of_materialising(tmp_path):
    path = _write(tmp_path)
    it = replay._read_jsonl(path)
    assert isinstance(it, types.GeneratorType)
    assert next(it)["uuid"] == "u0"


def test_read_jsonl_missing_file_yields_nothing(tmp_path):
    assert list(replay._read_jsonl(str(tmp_path / "nope.jsonl"))) == []


def _count_trims(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(replay, "_malloc_trim", lambda: calls.append)
    return calls


def test_replay_window_trims_heap_after_large_transcript(tmp_path, monkeypatch):
    path = _write(tmp_path)
    calls = _count_trims(monkeypatch)
    monkeypatch.setattr(replay, "_HEAP_TRIM_MIN_BYTES", 1)
    w = asyncio.run(replay.replay_window(path, limit=20))
    assert w.total_turns == 3
    assert calls == [0]
    asyncio.run(replay.replay_events(path))
    assert calls == [0, 0]


def test_small_transcript_skips_trim(tmp_path, monkeypatch):
    path = _write(tmp_path)
    calls = _count_trims(monkeypatch)
    asyncio.run(replay.replay_window(path, limit=20))   # default threshold: 8 MiB
    assert calls == []


def test_trim_is_noop_without_glibc_malloc_trim(tmp_path, monkeypatch):
    path = _write(tmp_path)
    monkeypatch.setattr(replay, "_malloc_trim", lambda: None)
    monkeypatch.setattr(replay, "_HEAP_TRIM_MIN_BYTES", 1)
    assert asyncio.run(replay.replay_window(path, limit=20)).total_turns == 3


def test_trim_still_runs_when_the_parse_raises(tmp_path, monkeypatch):
    path = _write(tmp_path)
    calls = _count_trims(monkeypatch)
    monkeypatch.setattr(replay, "_HEAP_TRIM_MIN_BYTES", 1)

    def boom(*a, **k):
        raise RuntimeError("parse failed")
    monkeypatch.setattr(replay, "_window", boom)
    try:
        asyncio.run(replay.replay_window(path, limit=20))
    except RuntimeError:
        pass
    assert calls == [0]


def test_real_malloc_trim_resolves_on_this_libc():
    # On glibc (CI, dobby, rocky) the symbol must actually resolve; otherwise the fix
    # silently degrades to a no-op. Other libcs are allowed to return None.
    import platform
    fn = replay._malloc_trim()
    if platform.libc_ver()[0] == "glibc":
        assert fn is not None
        assert fn(0) in (0, 1)
