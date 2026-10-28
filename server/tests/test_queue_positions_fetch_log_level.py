"""#3158: a transient daemon blip fetching queue positions must not log at WARNING."""
from __future__ import annotations

import logging

import pytest

from server.api import sessions as sessions_api
from server.ipc.client import DaemonTimeout

pytestmark = pytest.mark.asyncio


class _Orch:
    def __init__(self, exc: Exception | None = None, result=None):
        self._exc, self._result = exc, result

    async def session_queue_positions(self):
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.fixture(autouse=True)
def _fresh_throttle(monkeypatch):
    monkeypatch.setattr(sessions_api, "_QUEUE_POSITIONS_FAIL_LOG", sessions_api.LogThrottle())


async def test_daemon_unavailable_logs_below_warning(caplog):
    with caplog.at_level(logging.DEBUG, logger=sessions_api.log.name):
        out = await sessions_api._fetch_queue_positions(_Orch(DaemonTimeout("x")), "list_sessions")
    assert out == {}
    recs = [r for r in caplog.records if r.name == sessions_api.log.name]
    assert recs and all(r.levelno < logging.WARNING for r in recs)


async def test_other_errors_still_warn(caplog):
    with caplog.at_level(logging.DEBUG, logger=sessions_api.log.name):
        out = await sessions_api._fetch_queue_positions(_Orch(RuntimeError("boom")), "get_session")
    assert out == {}
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_returns_positions():
    assert await sessions_api._fetch_queue_positions(_Orch(result={"a": 1}), "x") == {"a": 1}
    assert await sessions_api._fetch_queue_positions(object(), "x") == {}
