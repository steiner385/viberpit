import asyncio
from datetime import datetime, timezone

import pytest

from server.api import accounts as accounts_api


def _snap(weekly_pct: float, provider="anthropic"):
    return accounts_api.QuotaSnapshot(
        provider=provider, fetched_at=datetime.now(timezone.utc), availability=accounts_api.AVAILABLE,
        windows=(accounts_api.QuotaWindow(key="session", label="session", used_pct=10.0),
                 accounts_api.QuotaWindow(key="weekly", label="weekly", used_pct=weekly_pct,
                                          resets_at=datetime(2026, 9, 23, 8, tzinfo=timezone.utc))))


@pytest.fixture
def fake_accounts(monkeypatch):
    infos = [{"name": "a", "enabled": True, "provider": "anthropic"},
             {"name": "b", "enabled": True, "provider": "anthropic"},
             {"name": "c", "enabled": False, "provider": "anthropic"},
             {"name": "d", "enabled": True, "provider": "openai"}]
    usages = {"a": (_snap(50.0), None), "b": (_snap(90.0), None), "c": (_snap(1.0), None),
              "d": (_snap(1.0, provider="openai"), None)}
    monkeypatch.setattr(accounts_api, "list_accounts", lambda: infos)

    async def gather(orch, names, timeout):
        return {n: usages[n] for n in names}
    monkeypatch.setattr(accounts_api, "_gather_usages", gather)


def test_headroom_applies_reserve_enabled_and_metering(fake_accounts):
    rows = asyncio.run(accounts_api.account_headroom(object(), reserve_pct=20))
    by = {r["name"]: r for r in rows}
    assert by["a"]["available"] is True
    assert by["b"]["available"] is False and by["b"]["reason"] == "at_ceiling"
    assert by["b"]["nextResetAt"].startswith("2026-09-23T08:00")
    assert by["c"]["available"] is False and by["c"]["reason"] == "disabled"
    assert by["d"]["metered"] is False and by["d"]["available"] is False
