"""#3150: three edge loop-stall causes the loop_lag watchdog caught on dobby.

A. ``GET /api/accounts`` took the ledger's ``LOCK_EX`` and fsync'd a republish
   of ledger.json on the loop, once per account, just to READ cached usage.
B. The first ``GET /api/models`` read + decoded the ~250 MB claude binary on
   the loop.
C. Full GCs paged a swapped-out startup heap back in; ``gc.freeze()`` moves
   that heap out of the collector's reach.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from datetime import datetime, timezone

import pytest

from server.api import accounts as acct_api


def _no_locked_ledger(monkeypatch):
    from viberpit import ledger

    @contextlib.contextmanager
    def _boom():
        raise AssertionError("a pure read must not take the ledger lock / write back")
        yield  # pragma: no cover

    monkeypatch.setattr(ledger, "locked_ledger", _boom)


def _seed_usage(monkeypatch, tmp_path, fetched_at: datetime) -> None:
    from viberpit import ledger

    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path))
    with ledger.locked_ledger() as led:
        led["accounts"]["acct"] = {"usage": {
            "session_pct": 42.0, "session_resets_at": None,
            "weekly_pct": 13.0, "weekly_resets_at": None,
            "extra_usage_enabled": False, "fetched_at": fetched_at.isoformat(),
        }}


# --- A. ledger reads --------------------------------------------------------

def test_cached_usage_read_takes_no_lock(monkeypatch, tmp_path):
    _seed_usage(monkeypatch, tmp_path, datetime.now(timezone.utc))
    _no_locked_ledger(monkeypatch)
    got = acct_api._local_cached_usage("acct")
    assert got is not None and got.window("session").used_pct == 42.0


def test_stale_usage_read_takes_no_lock(monkeypatch, tmp_path):
    from datetime import timedelta
    fetched = datetime.now(timezone.utc) - timedelta(minutes=30)
    _seed_usage(monkeypatch, tmp_path, fetched)
    _no_locked_ledger(monkeypatch)
    got = acct_api._local_stale_usage("acct")
    assert got is not None
    assert got[0].window("session").used_pct == 42.0


def test_safe_usage_reads_the_ledger_off_the_loop(monkeypatch):
    """Both ledger reads run in a worker thread, never on the loop thread."""
    seen: dict[str, threading.Thread] = {}

    def _cached(name):
        seen["cached"] = threading.current_thread()
        return None

    def _stale(name):
        seen["stale"] = threading.current_thread()
        return None, None

    class _Orch:
        async def usage(self, name):
            return None

    monkeypatch.setattr(acct_api, "_local_cached_usage", _cached)
    monkeypatch.setattr(acct_api, "_stale_fallback", _stale)

    async def _run():
        loop_thread = threading.current_thread()
        got = await acct_api._safe_usage(_Orch(), "a", 1.0)
        return loop_thread, got

    loop_thread, got = asyncio.run(_run())
    assert got == (None, None)
    assert seen["cached"] is not loop_thread
    assert seen["stale"] is not loop_thread


def test_account_defaults_reads_take_no_lock(monkeypatch, tmp_path):
    """The swept siblings: permission-mode and model-policy account defaults."""
    from viberpit import ledger

    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path))
    with ledger.locked_ledger() as led:
        led["accounts"]["acct"] = {"defaultPermissionMode": "auto"}
    _no_locked_ledger(monkeypatch)

    from server.automations.context import _account_defaults_for
    from server.sessions.permissions import resolve_permission_mode

    assert _account_defaults_for("acct")["defaultPermissionMode"] == "auto"
    assert asyncio.run(resolve_permission_mode(None, account="acct")) == "auto"


# --- B. model catalog -------------------------------------------------------

def test_binary_probe_finds_the_alias_array(tmp_path):
    from server.sessions import model_catalog

    blob = (b"\x00\xffjunk[\"sonnet\",\"x\"]more" +
            b'["sonnet","opus","opusplan","haiku"]' + b"\x00tail")
    p = tmp_path / "claude"
    p.write_bytes(blob)
    assert model_catalog._discover_from_binary(str(p)) == [
        "default", "sonnet", "opus", "opusplan", "haiku"]


@pytest.mark.parametrize("content", [None, b"", b"no aliases here"])
def test_binary_probe_degrades_to_none(tmp_path, content):
    from server.sessions import model_catalog

    p = tmp_path / "claude"
    if content is not None:
        p.write_bytes(content)
    assert model_catalog._discover_from_binary(str(p)) is None


_PICKER = b'["sonnet","opus","haiku","fable","best","sonnet[1m]","opus[1m]","fable[1m]","opusplan"]'
_PICKER_LIST = ["default", "sonnet", "opus", "haiku", "fable", "best",
                "sonnet[1m]", "opus[1m]", "fable[1m]", "opusplan"]


@pytest.mark.parametrize("split", range(1, len(b'"opusplan"')))
def test_binary_probe_finds_an_anchor_straddling_a_chunk_edge(monkeypatch, split):
    """#3300: the chunked find overlaps chunks, so no split of the anchor is missed."""
    from server.sessions import model_catalog

    monkeypatch.setattr(model_catalog, "_SCAN_CHUNK", 64)
    anchor_at = _PICKER.index(b'"opusplan"')
    blob = b"x" * (64 * 3 - anchor_at - split) + _PICKER + b"tail"
    assert blob.index(b'"opusplan"') + split == 64 * 3
    assert model_catalog._scan_aliases(blob) == _PICKER_LIST


def test_binary_probe_finds_an_array_longer_than_the_window(monkeypatch):
    """#3300: the regex window grows until it reaches the array's opening bracket."""
    from server.sessions import model_catalog

    monkeypatch.setattr(model_catalog, "_WINDOW", 16)
    many = ",".join(f'"sonnet-{i}"' for i in range(200)).encode()
    arr = b'["sonnet",' + many + b',"opusplan"]'
    got = model_catalog._scan_aliases(b"\x00junk[" + arr + b"\x00")
    assert got is not None and got[:2] == ["default", "sonnet"]
    assert got[-1] == "opusplan" and len(got) == 203


def test_binary_probe_skips_a_stray_anchor_outside_any_array(monkeypatch):
    from server.sessions import model_catalog

    monkeypatch.setattr(model_catalog, "_SCAN_CHUNK", 32)
    blob = b'x="opusplan";' + b"\x00" * 100 + _PICKER
    assert model_catalog._scan_aliases(blob) == _PICKER_LIST


def test_binary_scan_in_a_thread_does_not_stall_the_loop():
    """#3300: off-loop is not enough — ``re`` holds the GIL for the whole scan.

    The whole-file ``finditer`` this replaced delayed a concurrent 10 ms tick by
    the full scan time (5–6 s on dobby's 338 MB binary). The filler is ``["``
    pairs, which defeat the regex's literal-prefix fast path the way the real
    binary's JS does.
    """
    from server.sessions import model_catalog

    blob = b'["' * (32 * 1024 * 1024) + _PICKER

    async def _run() -> tuple[list[str] | None, float]:
        worst = 0.0
        task = asyncio.ensure_future(asyncio.to_thread(model_catalog._scan_aliases, blob))
        while not task.done():
            t0 = time.monotonic()
            await asyncio.sleep(0.01)
            worst = max(worst, time.monotonic() - t0 - 0.01)
        return await task, worst

    got, worst = asyncio.run(_run())
    assert got == _PICKER_LIST
    assert worst < 0.1, f"loop tick delayed {worst * 1000:.0f} ms by the scan thread"


def test_get_models_builds_the_catalog_off_the_loop(monkeypatch):
    from server.api import models as models_api
    from server.sessions import model_catalog

    seen: dict[str, threading.Thread] = {}

    def _catalog():
        seen["t"] = threading.current_thread()
        return [{"id": "sonnet", "label": "Sonnet"}]

    monkeypatch.setattr(model_catalog, "catalog", _catalog)

    async def _run():
        return threading.current_thread(), await models_api.get_models(None)

    loop_thread, got = asyncio.run(_run())
    assert got == [{"id": "sonnet", "label": "Sonnet"}]
    assert seen["t"] is not loop_thread


# --- C. gc.freeze -----------------------------------------------------------

def test_freeze_startup_heap_runs_once(monkeypatch):
    from server.obs import gc_freeze

    calls: list[str] = []
    monkeypatch.setattr(gc_freeze, "_frozen", False)
    monkeypatch.setattr(gc_freeze.gc, "collect", lambda: calls.append("collect"))
    monkeypatch.setattr(gc_freeze.gc, "freeze", lambda: calls.append("freeze"))

    assert gc_freeze.freeze_startup_heap() is True
    assert gc_freeze.freeze_startup_heap() is False
    assert calls == ["collect", "freeze"], "collect first so live garbage isn't frozen"


def test_freeze_after_startup_zero_delay_is_off(monkeypatch):
    from server.obs import gc_freeze

    called: list[bool] = []
    monkeypatch.setattr(gc_freeze, "freeze_startup_heap", lambda: called.append(True))
    monkeypatch.setenv("VIBERPIT_GC_FREEZE_DELAY_S", "0")
    asyncio.run(gc_freeze.freeze_after_startup())
    assert called == []

    asyncio.run(gc_freeze.freeze_after_startup(0.01))
    assert called == [True]
