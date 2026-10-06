"""send_prompt's no-driver result is logged at INFO; callers own the WARNING (#2697).

The ws.prompt bridge retries "no_driver" by design (rebuild driver, resend), so a
WARNING inside send_prompt auto-filed a recovered race as an error. Callers that
ignore the result log their own WARNING instead.
"""
from __future__ import annotations

import asyncio
import logging

from server.sessions.orchestrator import SessionOrchestrator


def _bare_orch() -> SessionOrchestrator:
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._draining = False
    orch._drivers = {}
    return orch


def test_no_driver_returns_no_driver_without_warning(caplog):
    orch = _bare_orch()
    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        result = asyncio.run(orch.send_prompt("s1", "hi"))
    assert result == "no_driver"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("no live driver" in r.getMessage() for r in caplog.records)
