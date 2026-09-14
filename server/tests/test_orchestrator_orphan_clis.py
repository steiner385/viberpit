"""Daemon startup enumeration must not block the event loop on the ledger."""
import threading
from unittest import mock

import pytest

from server.sessions import orchestrator as orch_mod
from server.sessions.orchestrator import ccswitch_accounts as accounts


@pytest.mark.asyncio
async def test_reap_orphan_clis_enumerates_accounts_off_event_loop(monkeypatch):
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    loop_thread = threading.get_ident()
    enumeration_threads = []

    def list_accounts():
        enumeration_threads.append(threading.get_ident())
        return [{"name": "primary"}]

    monkeypatch.setattr(accounts, "list_accounts", list_accounts)
    reap = mock.Mock(return_value=[])
    monkeypatch.setattr(orch_mod._orphan_cli, "reap_orphans", reap)
    assert await orch.reap_orphan_clis() == 0
    assert len(enumeration_threads) == 1
    assert enumeration_threads[0] != loop_thread
    reap.assert_called_once_with(["primary"])


@pytest.mark.asyncio
async def test_reap_orphan_clis_skips_reap_if_account_enumeration_fails(monkeypatch):
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    monkeypatch.setattr(accounts, "list_accounts",
                        mock.Mock(side_effect=OSError("ledger unavailable")))
    reap = mock.Mock()
    monkeypatch.setattr(orch_mod._orphan_cli, "reap_orphans", reap)
    assert await orch.reap_orphan_clis() == 0
    reap.assert_not_called()
