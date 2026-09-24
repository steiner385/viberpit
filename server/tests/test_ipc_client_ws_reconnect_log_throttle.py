"""A daemon that stays unreachable must not file one auto-error issue per retry.

``DaemonClient._reader`` re-enters the same ``except Exception`` branch on every
failed reconnect attempt. A daemon down for ~30h produced 1805 identical WARNINGs
(#1506) — ``server.obs.errsink`` captures WARNING+ and files one auto-error issue
per fingerprint, so the flood buried the one real signal (the daemon is down)
under its own repetitions.

The fix routes the record through ``server.obs.logdedupe`` — the repo's existing
answer to this shape (see ``test_host_probe_log_throttle.py`` for the sibling
fix), keyed per endpoint so a different daemon is never suppressed by this one.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from server.ipc import client as ipc_client
from server.ipc.client import DaemonClient


class _FailingConnect:
    """Fails ``async with daemon_ws_connect(...)`` on every attempt."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc_info):
        return False


@pytest.fixture(autouse=True)
def _fresh_throttle():
    ipc_client._WS_RECONNECT_LOG.clear()
    yield
    ipc_client._WS_RECONNECT_LOG.clear()


def _ws_error_records(caplog):
    return [r for r in caplog.records if "ipc: events WS error" in r.getMessage()]


@pytest.mark.asyncio
async def test_repeated_reconnect_failures_warn_once_and_demote_the_repeats(
        tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(
        ipc_client, "daemon_ws_connect",
        lambda endpoint, path, ssl_ctx=None: _FailingConnect(
            ConnectionRefusedError("connection refused")),
    )

    client = DaemonClient(str(tmp_path / "nonexistent.sock"))
    try:
        with caplog.at_level(logging.DEBUG, logger="server.ipc.client"):
            client.subscribe("s1")
            # backoff: 0.05, 0.1, 0.2, 0.4s — comfortably 4+ failures inside 1s.
            await asyncio.wait_for(
                _wait_for_n_records(caplog, _ws_error_records, 4), timeout=5)
    finally:
        await client.aclose()

    records = _ws_error_records(caplog)
    assert len(records) >= 4, "every failed attempt is still written somewhere"
    assert records[0].levelno == logging.WARNING
    assert all(r.levelno == logging.DEBUG for r in records[1:4])


@pytest.mark.asyncio
async def test_a_different_endpoint_is_not_suppressed_by_the_first(
        tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(
        ipc_client, "daemon_ws_connect",
        lambda endpoint, path, ssl_ctx=None: _FailingConnect(
            ConnectionRefusedError("connection refused")),
    )

    client_a = DaemonClient(str(tmp_path / "a.sock"))
    client_b = DaemonClient(str(tmp_path / "b.sock"))
    try:
        with caplog.at_level(logging.DEBUG, logger="server.ipc.client"):
            client_a.subscribe("s1")
            await asyncio.wait_for(
                _wait_for_n_records(caplog, _ws_error_records, 1), timeout=5)
            client_b.subscribe("s1")
            await asyncio.wait_for(
                _wait_for_n_records(caplog, _ws_error_records, 2), timeout=5)
    finally:
        await client_a.aclose()
        await client_b.aclose()

    warnings = [r for r in _ws_error_records(caplog) if r.levelno == logging.WARNING]
    assert len(warnings) == 2


async def _wait_for_n_records(caplog, selector, n: int) -> None:
    while len(selector(caplog)) < n:
        await asyncio.sleep(0.01)
