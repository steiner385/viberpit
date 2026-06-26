import types
from unittest.mock import AsyncMock

from server.api.sessions import _record_to_dict, _resolve_summary


def test_record_to_dict_includes_summary():
    rec = types.SimpleNamespace(
        id="s1", name="n", account="a", repo=None, status="in_progress",
        name_is_auto=True, detached=False, adopted_in_place=False, sub_agents=None,
        created_at=None, last_turn_at=None, account_pinned=False, model=None,
        active_goal=None, usage_unverified=False,
    )
    d = _record_to_dict(rec, summary="Refactoring auth")
    assert d["summary"] == "Refactoring auth"


def test_record_to_dict_summary_defaults_empty():
    rec = types.SimpleNamespace(
        id="s1", name="n", account="a", repo=None, status="idle",
        name_is_auto=True, detached=False, adopted_in_place=False, sub_agents=None,
        created_at=None, last_turn_at=None, account_pinned=False, model=None,
        active_goal=None, usage_unverified=False,
    )
    assert _record_to_dict(rec)["summary"] == ""


def test_resolve_summary_passes_viewing_and_tolerates_missing_method():
    import asyncio
    orch = types.SimpleNamespace(latest_summary=AsyncMock(return_value="Doing X"))
    rec = types.SimpleNamespace(id="s1")
    assert asyncio.run(_resolve_summary(orch, rec, True)) == "Doing X"
    orch.latest_summary.assert_awaited_once_with("s1", viewing=True)
    # Older shim without the method → "" (never raises)
    bare = types.SimpleNamespace()
    assert asyncio.run(_resolve_summary(bare, rec, True)) == ""
