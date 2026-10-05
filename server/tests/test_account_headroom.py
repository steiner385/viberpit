import asyncio
from datetime import datetime, timezone

import pytest

# QuotaWindow/anthropic_snapshot via tests.helpers (the real source, already
# imported there for `usage()`) rather than through server.api.accounts, which
# used to carry an unused re-export (suppressed as F401) solely so this file
# could reach them — and rather than a second copy of that import here, which
# the brand ratchet's `python-package` category has no headroom for (#2032).
from tests.helpers import QuotaWindow, anthropic_snapshot, register_accounts, temp_env
from server.api import accounts as accounts_api


def _snap(weekly_pct: float, provider="anthropic", session_pct: float = 10.0):
    # anthropic_snapshot (not a hand-rolled QuotaSnapshot) so the windows carry
    # duration_seconds — QuotaSnapshot.reserve_window() needs it to pick the
    # longest (weekly) window over session, exactly as the real read path does.
    if provider == "anthropic":
        return anthropic_snapshot(session_pct, None, weekly_pct,
                                  datetime(2026, 9, 23, 8, tzinfo=timezone.utc))
    return accounts_api.QuotaSnapshot(
        provider=provider, fetched_at=datetime.now(timezone.utc), availability=accounts_api.AVAILABLE,
        windows=(QuotaWindow(key="session", label="session", used_pct=session_pct),
                 QuotaWindow(key="weekly", label="weekly", used_pct=weekly_pct,
                            resets_at=datetime(2026, 9, 23, 8, tzinfo=timezone.utc))))


@pytest.fixture
def fake_accounts(monkeypatch):
    infos = [{"name": "a", "enabled": True, "provider": "anthropic"},
             {"name": "b", "enabled": True, "provider": "anthropic"},
             {"name": "c", "enabled": False, "provider": "anthropic"},
             {"name": "d", "enabled": True, "provider": "openai"},
             # Spec 2026-09-20 D4: overrides of the push level's reserve, both ways.
             {"name": "e", "enabled": True, "provider": "anthropic", "weeklyReservePct": 60},
             {"name": "f", "enabled": True, "provider": "anthropic", "weeklyReservePct": 5},
             # Fix round 1: the reserve binds only the reserve (weekly) window; the
             # hard ceiling binds every window. g is over its 60% override on the
             # SESSION window only (weekly is fresh) — a reserve must not trip on it.
             # h has no override but hits the hard ceiling (100%) on the session
             # window — that must still count, reserve or not.
             {"name": "g", "enabled": True, "provider": "anthropic", "weeklyReservePct": 60},
             {"name": "h", "enabled": True, "provider": "anthropic"}]
    usages = {"a": (_snap(50.0), None), "b": (_snap(90.0), None), "c": (_snap(1.0), None),
              "d": (_snap(1.0, provider="openai"), None),
              "e": (_snap(41.0), None), "f": (_snap(90.0), None),
              "g": (_snap(5.0, session_pct=45.0), None),
              "h": (_snap(5.0, session_pct=100.0), None)}
    monkeypatch.setattr(accounts_api, "list_accounts", lambda: infos)

    async def gather(orch, names, timeout):
        return {n: usages[n] for n in names}
    monkeypatch.setattr(accounts_api, "_gather_usages", gather)


def test_headroom_applies_reserve_enabled_and_metering(fake_accounts):
    rows = asyncio.run(accounts_api.account_headroom(object(), push_level="steady"))
    by = {r["name"]: r for r in rows}
    assert by["a"]["available"] is True and by["a"]["reservePct"] == 20
    assert by["b"]["available"] is False and by["b"]["reason"] == "over_reserve"
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
    assert by["e"]["available"] is False and by["e"]["reason"] == "over_reserve"
    assert by["e"]["reservePct"] == 60
    assert by["f"]["available"] is True and by["f"]["reservePct"] == 5


def test_headroom_default_follows_the_push_level(fake_accounts):
    rows = asyncio.run(accounts_api.account_headroom(object(), push_level="max"))
    by = {r["name"]: r for r in rows}
    assert by["b"]["available"] is True and by["b"]["reservePct"] == 5   # ceiling 95 > 90
    assert by["e"]["reservePct"] == 60                                    # the override still wins


def test_reserve_binds_only_the_reserve_window(fake_accounts):
    """Fix round 1 finding: g is over its 60% override (ceiling 40) on the SESSION
    window (45%), but the reserve binds only the reserve (weekly) window — which is
    fresh (5%) — so g stays available. Mirrors selector.rank_accounts' reserve leg,
    which reads only ``reserve_pct()`` (the reserve/weekly window)."""
    rows = asyncio.run(accounts_api.account_headroom(object(), push_level="steady"))
    by = {r["name"]: r for r in rows}
    assert by["g"]["available"] is True and by["g"]["reservePct"] == 60


def test_hard_ceiling_binds_every_window_regardless_of_reserve(fake_accounts):
    """h has no reserve override and is under the push level's reserve ceiling on
    every window, but its session usage (100%) is at the HARD ceiling — which,
    like the selector's exclusion rule, counts every window, not just the reserve
    one."""
    rows = asyncio.run(accounts_api.account_headroom(object(), push_level="steady"))
    by = {r["name"]: r for r in rows}
    assert by["h"]["available"] is False and by["h"]["reason"] == "at_ceiling"


def test_effective_reserve_pct_is_override_or_push_level():
    assert accounts_api.effective_reserve_pct({"weeklyReservePct": 60}, "steady") == 60.0
    assert accounts_api.effective_reserve_pct({"weeklyReservePct": None}, "steady") == 20.0
    assert accounts_api.effective_reserve_pct({}, "conserve") == 40.0
    assert accounts_api.effective_reserve_pct({}, "bogus") == 20.0


def test_headroom_engine_uses_canonical_registry_resolution(monkeypatch):
    """#2032 deferred-minor: ``engine`` must come from
    ``account_registry.entry(name).engine`` (the same resolution
    ``account_to_wire`` uses), never a ``provider == "openai"`` guess — a Copilot
    account is ALSO provider "openai" and that heuristic would misclassify it as
    codex."""
    with temp_env():
        register_accounts("codex-acct", provider="openai", engine="codex")
        register_accounts("plain-acct", provider="anthropic")
        infos = [{"name": "codex-acct", "enabled": True, "provider": "openai"},
                 {"name": "plain-acct", "enabled": True, "provider": "anthropic"},
                 # Not in the manifest at all — must still resolve to the engine
                 # registry's default, never raise.
                 {"name": "unregistered", "enabled": True, "provider": "anthropic"}]
        monkeypatch.setattr(accounts_api, "list_accounts", lambda: infos)

        async def gather(orch, names, timeout):
            return {n: (None, None) for n in names}
        monkeypatch.setattr(accounts_api, "_gather_usages", gather)

        rows = asyncio.run(accounts_api.account_headroom(object(), push_level="steady"))
        by = {r["name"]: r for r in rows}
        assert by["codex-acct"]["engine"] == "codex"
        assert by["plain-acct"]["engine"] == "claude_code"
        assert by["unregistered"]["engine"] == "claude_code"


def _fable_exhausted(weekly_pct: float = 70.0):
    """An account mid-way through its account-wide weekly but OUT on the
    Fable-only weekly window — the #2684 shape (four accounts at 63–76% weekly,
    all at 100% Fable)."""
    from dataclasses import replace
    from viberpit.quota import SCOPE_MODEL
    base = _snap(weekly_pct)
    fable = QuotaWindow(key="weekly:fable", label="Fable weekly", used_pct=100.0,
                        resets_at=datetime(2026, 10, 8, 16, tzinfo=timezone.utc),
                        duration_seconds=7 * 24 * 3600, scope=SCOPE_MODEL,
                        applies_to_models=("fable",))
    return replace(base, windows=(*base.windows, fable))


def test_a_model_scoped_window_never_zeroes_the_account(monkeypatch):
    """#2684: the governor read headroom with no model, and an unknown model
    matches EVERY window — so a Fable-only window at 100% marked every Claude
    account at_ceiling and the runner's fleet collapsed to its in-flight floor.
    Account-level headroom is judged on account-scoped windows; the exhausted
    model is reported on the row instead (the spawn-time selector is already
    model-aware, so a Fable spawn still skips the account)."""
    monkeypatch.setattr(accounts_api, "list_accounts",
                        lambda: [{"name": "a", "enabled": True, "provider": "anthropic"}])

    async def gather(orch, names, timeout):
        return {"a": (_fable_exhausted(), None)}
    monkeypatch.setattr(accounts_api, "_gather_usages", gather)
    [row] = asyncio.run(accounts_api.account_headroom(object(), push_level="steady"))
    assert row["available"] is True and row["reason"] is None
    assert row["exhaustedModels"] == [{"model": "fable", "resetsAt": "2026-10-08T16:00:00+00:00"}]


def test_an_account_wide_window_at_the_hard_ceiling_still_binds(monkeypatch):
    monkeypatch.setattr(accounts_api, "list_accounts",
                        lambda: [{"name": "a", "enabled": True, "provider": "anthropic"}])

    async def gather(orch, names, timeout):
        return {"a": (_fable_exhausted(weekly_pct=100.0), None)}
    monkeypatch.setattr(accounts_api, "_gather_usages", gather)
    [row] = asyncio.run(accounts_api.account_headroom(object(), push_level="steady"))
    assert row["available"] is False and row["reason"] == "at_ceiling"
