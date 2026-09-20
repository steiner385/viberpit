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
             {"name": "d", "enabled": True, "provider": "openai"},
             # Spec 2026-09-20 D4: overrides of the push level's reserve, both ways.
             {"name": "e", "enabled": True, "provider": "anthropic", "weeklyReservePct": 60},
             {"name": "f", "enabled": True, "provider": "anthropic", "weeklyReservePct": 5}]
    usages = {"a": (_snap(50.0), None), "b": (_snap(90.0), None), "c": (_snap(1.0), None),
              "d": (_snap(1.0, provider="openai"), None),
              "e": (_snap(41.0), None), "f": (_snap(90.0), None)}
    monkeypatch.setattr(accounts_api, "list_accounts", lambda: infos)

    async def gather(orch, names, timeout):
        return {n: usages[n] for n in names}
    monkeypatch.setattr(accounts_api, "_gather_usages", gather)


def test_headroom_applies_reserve_enabled_and_metering(fake_accounts):
    rows = asyncio.run(accounts_api.account_headroom(object(), push_level="steady"))
    by = {r["name"]: r for r in rows}
    assert by["a"]["available"] is True and by["a"]["reservePct"] == 20
    assert by["b"]["available"] is False and by["b"]["reason"] == "at_ceiling"
    assert by["b"]["nextResetAt"].startswith("2026-09-23T08:00")
    assert by["c"]["available"] is False and by["c"]["reason"] == "disabled"
    assert by["d"]["metered"] is False and by["d"]["available"] is False


def test_headroom_honours_a_per_account_override_both_ways(fake_accounts):
    """Spec 2026-09-20 §9: an account with a 60% override is unavailable at 41% used
    while the push level is steady (its ceiling is 40, not 80); one with a 5%
    override is still available at 90% used (ceiling 95). The row says which
    reserve it was judged by."""
    rows = asyncio.run(accounts_api.account_headroom(object(), push_level="steady"))
    by = {r["name"]: r for r in rows}
    assert by["e"]["available"] is False and by["e"]["reason"] == "at_ceiling"
    assert by["e"]["reservePct"] == 60
    assert by["f"]["available"] is True and by["f"]["reservePct"] == 5


def test_headroom_default_follows_the_push_level(fake_accounts):
    rows = asyncio.run(accounts_api.account_headroom(object(), push_level="max"))
    by = {r["name"]: r for r in rows}
    assert by["b"]["available"] is True and by["b"]["reservePct"] == 5   # ceiling 95 > 90
    assert by["e"]["reservePct"] == 60                                    # the override still wins


def test_effective_reserve_pct_is_override_or_push_level():
    assert accounts_api.effective_reserve_pct({"weeklyReservePct": 60}, "steady") == 60.0
    assert accounts_api.effective_reserve_pct({"weeklyReservePct": None}, "steady") == 20.0
    assert accounts_api.effective_reserve_pct({}, "conserve") == 40.0
