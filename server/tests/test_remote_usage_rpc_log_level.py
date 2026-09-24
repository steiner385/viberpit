"""Pin the level of RemoteOrchestrator.usage()'s "unavailable" log line (#1576, #1684).

Error Triage files every WARNING as an issue. A per-account usage RPC blip was logged
at WARNING, unthrottled, on every dashboard load, and was filed as a recurring incident
twice (#1576, #1684). #2014 moved it to INFO: `_safe_usage`'s stale-ledger fallback
already absorbs the blip, and a real pool wedge is reported once, throttled, by the
client's `_note_pool_timeout`. No test pinned that level, so this one does. It fails
against the old `log.warning` call.
"""
from __future__ import annotations

import asyncio
import logging

from server.ipc.client import DaemonUnavailable
from server.sessions.remote import RemoteOrchestrator


class _UnavailableClient:
    async def call(self, method, **kwargs):
        raise DaemonUnavailable("connection reset")


def test_usage_rpc_unavailable_logs_info_and_returns_none(caplog):
    orch = RemoteOrchestrator(_UnavailableClient())
    with caplog.at_level(logging.DEBUG, logger="server.sessions.remote"):
        result = asyncio.run(orch.usage("tony_cairnea.com"))

    assert result is None
    records = [r for r in caplog.records
               if r.name == "server.sessions.remote" and "unavailable" in r.getMessage()]
    assert [r.levelname for r in records] == ["INFO"]
    assert "tony_cairnea.com" in records[0].getMessage()
