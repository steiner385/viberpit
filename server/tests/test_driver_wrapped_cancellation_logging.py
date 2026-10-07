"""Driver-wrapped cancellations and DB connect blips are not auto-errors (#2751, #2752).

asyncpg/uvloop catch a cancellation mid TLS handshake and raise a concrete
exception ``from`` it. Two orchestrator sites logged that at WARNING/ERROR, which
is what files an auto-error issue. Each quiet case is paired with a loud one so
the classifier is proven able to fail.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.exc import ProgrammingError

import server.sessions.orchestrator as mod
from server.sessions.orchestrator import SessionOrchestrator, _log_bg_exception


def _wrapped_cancellation() -> Exception:
    """The asyncpg/uvloop shape: ``raise ConcreteError(...) from CancelledError()``."""
    try:
        try:
            raise asyncio.CancelledError()
        except asyncio.CancelledError as cancelled:
            raise ConnectionResetError("handshake aborted") from cancelled
    except ConnectionResetError as exc:
        return exc


def _wrapped_timeout() -> Exception:
    try:
        try:
            raise TimeoutError()
        except TimeoutError as timeout:
            raise RuntimeError("pool checkout failed") from timeout
    except RuntimeError as exc:
        return exc


def _sql_error() -> Exception:
    return ProgrammingError("SELECT name FROM hosts", {}, Exception("no such column"))


def _records_at(caplog, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == mod.log.name and r.levelno >= level]


# --------------------------------------------------------------------------- #
# _log_bg_exception (#2751)                                                    #
# --------------------------------------------------------------------------- #

def _done_task(*, cancel: bool) -> asyncio.Task:
    """A task that dies by a driver-wrapped CancelledError, with or without a
    ``cancel()`` on the task itself."""
    async def victim(started: asyncio.Event):
        started.set()
        try:
            await asyncio.sleep(0 if not cancel else 10)
            raise asyncio.CancelledError()  # a driver-internal cancellation
        except asyncio.CancelledError as cancelled:
            raise ConnectionResetError("handshake aborted") from cancelled

    async def go():
        started = asyncio.Event()
        task = asyncio.create_task(victim(started), name="pm-classify-ingest-viberpit")
        await started.wait()
        if cancel:
            task.cancel()
        await asyncio.wait({task})
        return task

    return asyncio.run(go())


def test_bg_exception_driver_wrapped_cancellation_is_not_an_error(caplog):
    task = _done_task(cancel=True)
    assert not task.cancelled()  # the shape that slipped past the old guard
    with caplog.at_level(logging.DEBUG, logger=mod.log.name):
        _log_bg_exception(task)
    assert _records_at(caplog, logging.WARNING) == []


def test_bg_exception_wrapped_cancellation_without_cancel_still_errors(caplog):
    """No cancel() on THIS task: the driver's own cancellation is a real failure."""
    task = _done_task(cancel=False)
    with caplog.at_level(logging.DEBUG, logger=mod.log.name):
        _log_bg_exception(task)
    assert len(_records_at(caplog, logging.ERROR)) == 1


def test_bg_exception_plain_failure_still_logs_error(caplog):
    async def boom():
        raise RuntimeError("real bug")

    async def go():
        task = asyncio.create_task(boom(), name="pm-classify-ingest-viberpit")
        await asyncio.wait({task})
        return task

    task = asyncio.run(go())
    with caplog.at_level(logging.DEBUG, logger=mod.log.name):
        _log_bg_exception(task)
    errors = _records_at(caplog, logging.ERROR)
    assert len(errors) == 1
    assert "pm-classify-ingest-viberpit" in errors[0].getMessage()


# --------------------------------------------------------------------------- #
# _enabled_host_names (#2752)                                                  #
# --------------------------------------------------------------------------- #

def _failing_db_session(exc: Exception):
    @asynccontextmanager
    async def _session():
        raise exc
        yield  # pragma: no cover

    return _session


def _orch(cached: frozenset[str] | None) -> SessionOrchestrator:
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    if cached is not None:
        # Infinitely stale so the TTL short-circuit doesn't skip the read.
        orch._ownership_hosts_cache = (float("-inf"), cached)
    return orch


@pytest.mark.parametrize("make_exc", [_wrapped_cancellation, _wrapped_timeout])
@pytest.mark.parametrize("cached", [frozenset({"dobby"}), None])
def test_enabled_hosts_transient_failure_degrades_quietly(monkeypatch, caplog, make_exc, cached):
    monkeypatch.setattr(mod, "db_session", _failing_db_session(make_exc()))
    with caplog.at_level(logging.DEBUG, logger=mod.log.name):
        got = asyncio.run(_orch(cached)._enabled_host_names())
    assert got == cached
    assert _records_at(caplog, logging.WARNING) == []


@pytest.mark.parametrize("cached", [frozenset({"dobby"}), None])
def test_enabled_hosts_sql_error_still_warns(monkeypatch, caplog, cached):
    monkeypatch.setattr(mod, "db_session", _failing_db_session(_sql_error()))
    with caplog.at_level(logging.DEBUG, logger=mod.log.name):
        got = asyncio.run(_orch(cached)._enabled_host_names())
    assert got == cached
    warnings = _records_at(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None


def test_enabled_hosts_propagates_our_own_wrapped_cancellation(monkeypatch):
    """When THIS task was cancelled and the driver wrapped it, re-raise the
    cancellation instead of degrading and running on (cancel rule, #1164)."""
    started = asyncio.Event()

    @asynccontextmanager
    async def _session():
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError as cancelled:
            raise ConnectionResetError("handshake aborted") from cancelled
        yield  # pragma: no cover

    monkeypatch.setattr(mod, "db_session", _session)

    async def go():
        task = asyncio.create_task(_orch(frozenset({"dobby"}))._enabled_host_names())
        await started.wait()
        task.cancel()
        await asyncio.wait({task})
        return task

    assert asyncio.run(go()).cancelled()
