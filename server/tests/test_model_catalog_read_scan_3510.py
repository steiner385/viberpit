"""#3510: the model-catalog binary scan must not stall the loop on a cold disk.

#3311 chunked an ``mmap.find``, but a page fault holds the GIL, so after a deploy
(saturated disk) one 4 MiB chunk froze the event loop for seconds. The scan now
reads with ``readinto`` (GIL released while blocked) and skips entirely when the
binary is unchanged since the last probe.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import time

import pytest

from server.sessions import model_catalog as mc

_PICKER = b'["sonnet","opus","haiku","fable","best","sonnet[1m]","opus[1m]","fable[1m]","opusplan"]'
_PICKER_LIST = ["default", "sonnet", "opus", "haiku", "fable", "best",
                "sonnet[1m]", "opus[1m]", "fable[1m]", "opusplan"]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(mc, "_persist_path", lambda: tmp_path / "cache" / "model-catalog.json")
    monkeypatch.setattr(mc, "_cache", None)


class _SlowDisk(io.BytesIO):
    """A file whose every read blocks like a saturated disk — with the GIL
    released, exactly as a real ``read`` syscall releases it."""

    def readinto(self, b):  # type: ignore[override]
        time.sleep(0.05)
        return super().readinto(b)


def test_slow_disk_reads_do_not_stall_the_loop(monkeypatch):
    monkeypatch.setattr(mc, "_SCAN_CHUNK", 64 * 1024)
    src = _SlowDisk(b"\x00" * (40 * 64 * 1024) + _PICKER + b"tail")

    async def _run() -> tuple[list[str] | None, float, float]:
        worst = 0.0
        t_start = time.monotonic()
        task = asyncio.ensure_future(asyncio.to_thread(mc._scan_aliases, src))
        while not task.done():
            t0 = time.monotonic()
            await asyncio.sleep(0.01)
            worst = max(worst, time.monotonic() - t0 - 0.01)
        return await task, worst, time.monotonic() - t_start

    got, worst, total = asyncio.run(_run())
    assert got == _PICKER_LIST
    assert total > 1.0, "the slow disk should have made the scan itself slow"
    assert worst < 0.1, f"loop tick delayed {worst * 1000:.0f} ms by a blocked read"


def test_discover_does_not_mmap_the_binary(monkeypatch, tmp_path):
    import mmap

    def _boom(*_a, **_k):
        raise AssertionError("mmap.find faults pages in with the GIL held (#3510)")

    monkeypatch.setattr(mmap, "mmap", _boom)
    p = tmp_path / "claude"
    p.write_bytes(b"\x00junk" + _PICKER + b"\x00")
    assert mc._discover_from_binary(str(p)) == _PICKER_LIST


def test_scan_finds_a_hit_behind_the_scan_buffer(monkeypatch):
    """The array may open in an earlier chunk than the anchor; the window is
    re-read from the file, not sliced from the current buffer."""
    monkeypatch.setattr(mc, "_SCAN_CHUNK", 16)
    monkeypatch.setattr(mc, "_WINDOW", 8)
    blob = b"x" * 37 + _PICKER + b"y" * 50
    assert mc._scan_aliases(io.BytesIO(blob)) == _PICKER_LIST


# --- persisted probe result -------------------------------------------------

def _binary(tmp_path, body=_PICKER):
    p = tmp_path / "claude"
    p.write_bytes(b"\x00junk" + body + b"\x00")
    return p


def _counting_discover(monkeypatch) -> list[str]:
    calls: list[str] = []
    real = mc._discover_from_binary

    def _spy(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(mc, "_discover_from_binary", _spy)
    return calls


def test_unchanged_binary_skips_the_scan(monkeypatch, tmp_path):
    p = _binary(tmp_path)
    calls = _counting_discover(monkeypatch)
    assert mc._probe(str(p)) == _PICKER_LIST
    assert mc._probe(str(p)) == _PICKER_LIST
    assert len(calls) == 1


def test_persisted_result_survives_a_process_restart(monkeypatch, tmp_path):
    """The point of persisting: a fresh process (empty in-memory cache) on the
    same binary reads the stored list instead of scanning."""
    p = _binary(tmp_path)
    monkeypatch.setattr(mc, "resolve_claude_bin", lambda: str(p))
    calls = _counting_discover(monkeypatch)
    mc._live_aliases()
    mc._cache = None  # "restart"
    assert mc._live_aliases() == _PICKER_LIST
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["size", "mtime", "path"])
def test_changed_binary_rescans(monkeypatch, tmp_path, change):
    p = _binary(tmp_path)
    calls = _counting_discover(monkeypatch)
    mc._probe(str(p))
    if change == "size":
        st = p.stat()
        p.write_bytes(p.read_bytes() + b"\x00more")
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    elif change == "mtime":
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    else:
        other = tmp_path / "claude2"
        other.write_bytes(p.read_bytes())
        os.utime(other, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns))
        p = other
    assert mc._probe(str(p)) == _PICKER_LIST
    assert len(calls) == 2


def test_symlink_to_same_binary_hits(monkeypatch, tmp_path):
    p = _binary(tmp_path)
    link = tmp_path / "claude-link"
    link.symlink_to(p)
    calls = _counting_discover(monkeypatch)
    mc._probe(str(p))
    assert mc._probe(str(link)) == _PICKER_LIST
    assert len(calls) == 1


def test_failed_probe_is_not_persisted(monkeypatch, tmp_path):
    p = tmp_path / "claude"
    p.write_bytes(b"no aliases here")
    assert mc._probe(str(p)) is None
    assert not mc._persist_path().exists()


@pytest.mark.parametrize("content", ["not json", "[]", '{"key": 1}',
                                     '{"key": null, "aliases": ["opus"]}'])
def test_corrupt_persisted_file_falls_back_to_a_scan(monkeypatch, tmp_path, content):
    p = _binary(tmp_path)
    mc._persist_path().parent.mkdir(parents=True)
    mc._persist_path().write_text(content)
    calls = _counting_discover(monkeypatch)
    assert mc._probe(str(p)) == _PICKER_LIST
    assert len(calls) == 1
    assert json.loads(mc._persist_path().read_text())["aliases"] == _PICKER_LIST


def test_refresh_drops_the_persisted_result(monkeypatch, tmp_path):
    p = _binary(tmp_path)
    calls = _counting_discover(monkeypatch)
    mc._probe(str(p))
    mc.refresh()
    assert not mc._persist_path().exists()
    mc._probe(str(p))
    assert len(calls) == 2
