"""A daemon that stays unreachable must not file one auto-error issue per retry.

``DaemonClient._reader`` re-enters the same ``except Exception`` branch on every
failed reconnect attempt. A daemon down for ~30h produced 1805 identical WARNINGs
(#1506) — ``server.obs.errsink`` captures WARNING+ and files one auto-error issue
per fingerprint, so the flood buried the one real signal (the daemon is down)
under its own repetitions.

The fix routes the record through ``server.obs.logdedupe`` — the repo's existing
answer to this shape (see ``test_host_probe_log_throttle.py`` for the sibling
fix), keyed per endpoint so a different daemon is never suppressed by this one.

A second, narrower flood has the same symptom but a different cause (#1662):
uvicorn closes every open websocket with code 1012 (SERVICE_RESTART) on its own
lifespan shutdown, i.e. on every deploy of the daemon it is talking to. That is
expected operational behaviour, not a signal anything is wrong, but throttling
alone still let the *first* occurrence of each 6h window through at WARNING —
indistinguishable from a real outage in the error sink. Those reconnects log at
INFO instead.

A third variant has the same cause but a different exception shape (#1743): the
*next* reconnect attempt after a SERVICE_RESTART close can land while the new
daemon process is still starting up, so `websockets` raises a bare
`TimeoutError("timed out during opening handshake")` instead of a clean close.
A same-typed `TimeoutError` from a hang on an already-established connection
("timed out while closing connection") is a real-problem shape and must keep
warning.

The same handshake timeout, or a refused connect, is also the transient blip
(#1742) that the very next retry heals. Neither is an outage while it is brief,
so these connect failures log at INFO until the link has been down for
``_RECONNECT_WARN_AFTER_S``; only a sustained outage — including a daemon wedged
in startup that times out forever — escalates to WARNING.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
import websockets

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


@pytest.fixture
def _no_grace(monkeypatch):
    """Escalate on the first failure, for tests about the throttle itself."""
    monkeypatch.setattr(ipc_client, "_RECONNECT_WARN_AFTER_S", 0.0)


def _ws_error_records(caplog):
    return [r for r in caplog.records if "ipc: events WS error" in r.getMessage()]


@pytest.mark.usefixtures("_no_grace")
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


@pytest.mark.usefixtures("_no_grace")
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


@pytest.mark.asyncio
async def test_service_restart_close_reconnect_logs_at_info_not_warning(
        tmp_path, caplog, monkeypatch):
    close = websockets.frames.Close(
        websockets.CloseCode.SERVICE_RESTART, "service restart")
    monkeypatch.setattr(
        ipc_client, "daemon_ws_connect",
        lambda endpoint, path, ssl_ctx=None: _FailingConnect(
            websockets.ConnectionClosed(close, close, rcvd_then_sent=True)),
    )

    client = DaemonClient(str(tmp_path / "nonexistent.sock"))
    try:
        with caplog.at_level(logging.DEBUG, logger="server.ipc.client"):
            client.subscribe("s1")
            await asyncio.wait_for(
                _wait_for_n_records(caplog, _ws_error_records, 1), timeout=5)
    finally:
        await client.aclose()

    records = _ws_error_records(caplog)
    assert records, "the reconnect is still logged somewhere"
    assert records[0].levelno == logging.INFO
    assert not any(r.levelno == logging.WARNING for r in records)


@pytest.mark.asyncio
async def test_opening_handshake_timeout_inside_the_grace_window_logs_at_info(
        tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(
        ipc_client, "daemon_ws_connect",
        lambda endpoint, path, ssl_ctx=None: _FailingConnect(
            TimeoutError("timed out during opening handshake")),
    )

    client = DaemonClient(str(tmp_path / "nonexistent.sock"))
    try:
        with caplog.at_level(logging.DEBUG, logger="server.ipc.client"):
            client.subscribe("s1")
            await asyncio.wait_for(
                _wait_for_n_records(caplog, _ws_error_records, 3), timeout=5)
    finally:
        await client.aclose()

    records = _ws_error_records(caplog)
    assert records[0].levelno == logging.INFO
    assert not any(r.levelno >= logging.WARNING for r in records)


@pytest.mark.asyncio
async def test_closing_connection_timeout_reconnect_still_warns(
        tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(
        ipc_client, "daemon_ws_connect",
        lambda endpoint, path, ssl_ctx=None: _FailingConnect(
            TimeoutError("timed out while closing connection")),
    )

    client = DaemonClient(str(tmp_path / "nonexistent.sock"))
    try:
        with caplog.at_level(logging.DEBUG, logger="server.ipc.client"):
            client.subscribe("s1")
            await asyncio.wait_for(
                _wait_for_n_records(caplog, _ws_error_records, 1), timeout=5)
    finally:
        await client.aclose()

    records = _ws_error_records(caplog)
    assert records, "the reconnect is still logged somewhere"
    assert records[0].levelno == logging.WARNING


@pytest.mark.asyncio
async def test_outage_past_the_grace_window_escalates_to_warning(
        tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(ipc_client, "_RECONNECT_WARN_AFTER_S", 0.2)
    monkeypatch.setattr(
        ipc_client, "daemon_ws_connect",
        lambda endpoint, path, ssl_ctx=None: _FailingConnect(
            TimeoutError("timed out during opening handshake")),
    )

    def _warnings(caplog):
        return [r for r in _ws_error_records(caplog) if r.levelno == logging.WARNING]

    client = DaemonClient(str(tmp_path / "nonexistent.sock"))
    try:
        with caplog.at_level(logging.DEBUG, logger="server.ipc.client"):
            client.subscribe("s1")
            await asyncio.wait_for(_wait_for_n_records(caplog, _warnings, 1), timeout=5)
    finally:
        await client.aclose()

    records = _ws_error_records(caplog)
    # The blip reported at INFO first, and spending that did NOT consume the
    # WARNING window — the sustained outage still reaches the error sink.
    assert records[0].levelno == logging.INFO
    assert len(_warnings(caplog)) == 1


def test_reconnect_log_level_policy(monkeypatch):
    monkeypatch.setattr(ipc_client, "_RECONNECT_WARN_AFTER_S", 60.0)
    for blip in (TimeoutError("timed out during opening handshake"),
                 ConnectionRefusedError("connection refused")):
        assert ipc_client._reconnect_log_level(blip, 0.0) == logging.INFO
        assert ipc_client._reconnect_log_level(blip, 59.9) == logging.INFO
        assert ipc_client._reconnect_log_level(blip, 60.0) == logging.WARNING
    close = websockets.frames.Close(websockets.CloseCode.SERVICE_RESTART, "restart")
    restart = websockets.ConnectionClosed(close, close, rcvd_then_sent=True)
    assert ipc_client._reconnect_log_level(restart, 3600.0) == logging.INFO
    # A hang on an established connection is never a connect blip (#1743).
    hang = TimeoutError("timed out while closing connection")
    assert ipc_client._reconnect_log_level(hang, 0.0) == logging.WARNING


async def _wait_for_n_records(caplog, selector, n: int) -> None:
    while len(selector(caplog)) < n:
        await asyncio.sleep(0.01)
