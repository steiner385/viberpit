from unittest.mock import patch
import pytest

from ccswitch.config import Config
from ccswitch.selector import AllAccountsBusy, Picked
from ccswitch import accounts
from tests.helpers import temp_env


def _legacy_entries():
    from ccswitch import account_registry

    return [
        account_registry.entry_for(name, account_registry.PROVIDER_ANTHROPIC)
        for name in accounts._account_names()
    ]


def _patch_legacy_entries(monkeypatch):
    monkeypatch.setattr(accounts.account_registry, "all_entries", _legacy_entries)


def test_pick_account_detailed_optimistic_when_usage_none(monkeypatch):
    monkeypatch.setattr(accounts, "_account_names", lambda: ["a", "b"])
    _patch_legacy_entries(monkeypatch)
    with patch.object(accounts, "usage_for", return_value=None), \
         patch("ccswitch.ledger.get_status", return_value="ok"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.reserve"):
        picked = accounts.pick_account_detailed(Config(), load={"a": 1.0, "b": 0.0})
    assert isinstance(picked, Picked)
    assert picked == Picked(name="b", optimistic=True)


def test_pick_account_detailed_skips_needs_reauth(monkeypatch):
    from ccswitch.selector import AllAccountsBusy
    import pytest
    monkeypatch.setattr(accounts, "_account_names", lambda: ["a"])
    _patch_legacy_entries(monkeypatch)
    with patch.object(accounts, "usage_for", return_value=None), \
         patch("ccswitch.ledger.get_status", return_value="needs-reauth"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.reserve"):
        with pytest.raises(AllAccountsBusy) as ei:
            accounts.pick_account_detailed(Config())
    assert ei.value.kind == "no_usable_accounts"


def test_pick_account_str_wrapper_returns_name(monkeypatch):
    monkeypatch.setattr(accounts, "_account_names", lambda: ["a"])
    _patch_legacy_entries(monkeypatch)
    with patch.object(accounts, "usage_for", return_value=None), \
         patch("ccswitch.ledger.get_status", return_value="ok"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.reserve"):
        assert accounts.pick_account(Config()) == "a"


def test_pick_account_wrapper_never_selects_a_codex_account():
    from ccswitch import account_registry
    from ccswitch.engines import AgentEngine

    with temp_env():
        account_registry.register_account(
            "openai-main",
            account_registry.PROVIDER_OPENAI,
            engine=AgentEngine.CODEX.value,
        )
        with patch.object(accounts, "usage_for", return_value=None) as usage_for:
            with pytest.raises(AllAccountsBusy) as exc_info:
                accounts.pick_account(Config())

    assert exc_info.value.kind == "no_usable_accounts"
    usage_for.assert_not_called()


def test_pick_account_detailed_reserve_false_does_not_reserve(monkeypatch):
    """A read-only availability probe (reserve=False) must NOT write a reservation —
    reserving on a check starves real spawns/creates when only one account has room."""
    monkeypatch.setattr(accounts, "_account_names", lambda: ["a"])
    _patch_legacy_entries(monkeypatch)
    with patch.object(accounts, "usage_for", return_value=None), \
         patch("ccswitch.ledger.get_status", return_value="ok"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.reserve") as reserve:
        picked = accounts.pick_account_detailed(Config(), reserve=False)
    assert picked == Picked(name="a", optimistic=True)
    reserve.assert_not_called()


def test_pick_account_detailed_reserve_true_still_reserves(monkeypatch):
    monkeypatch.setattr(accounts, "_account_names", lambda: ["a"])
    _patch_legacy_entries(monkeypatch)
    with patch.object(accounts, "usage_for", return_value=None), \
         patch("ccswitch.ledger.get_status", return_value="ok"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.reserve") as reserve:
        accounts.pick_account_detailed(Config())  # reserve defaults to True
    reserve.assert_called_once()


def test_pick_account_detailed_skips_disabled(monkeypatch):
    """A disabled account is never selected — blocked everywhere, in any tier."""
    monkeypatch.setattr(accounts, "_account_names", lambda: ["a", "b"])
    _patch_legacy_entries(monkeypatch)
    enabled = {"a": False, "b": True}
    with patch.object(accounts, "usage_for", return_value=None) as usage_for, \
         patch("ccswitch.ledger.is_account_enabled", side_effect=lambda led, n: enabled[n]), \
         patch("ccswitch.ledger.get_status", return_value="ok"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.reserve"):
        picked = accounts.pick_account_detailed(Config())
    assert picked.name == "b"
    # A disabled account must not even be polled for usage (paused == left alone).
    usage_for.assert_called_once_with("b")


def test_pick_account_detailed_all_disabled_raises(monkeypatch):
    from ccswitch.selector import AllAccountsBusy
    import pytest
    monkeypatch.setattr(accounts, "_account_names", lambda: ["a"])
    _patch_legacy_entries(monkeypatch)
    with patch.object(accounts, "usage_for", return_value=None) as usage_for, \
         patch("ccswitch.ledger.is_account_enabled", return_value=False), \
         patch("ccswitch.ledger.reserve"):
        with pytest.raises(AllAccountsBusy) as ei:
            accounts.pick_account_detailed(Config())
    assert ei.value.kind == "no_usable_accounts"
    usage_for.assert_not_called()


def test_pick_account_detailed_respects_weekly_reserve(monkeypatch):
    """An over-reserve account is demoted: the pick flips once a reserve is set."""
    from tests.helpers import usage
    monkeypatch.setattr(accounts, "_account_names", lambda: ["hot", "cool"])
    _patch_legacy_entries(monkeypatch)
    # Both windows reset in 2h (helpers default). Perishability = binding-dimension
    # headroom / hours: hot = 5%/2h = 2.5/h beats cool = 4%/2h = 2/h, so hot wins
    # WITHOUT a reserve — the flip below is attributable to the reserve alone.
    usages = {
        "hot": usage(session=10.0, weekly=95.0),
        "cool": usage(session=10.0, weekly=96.0),
    }
    common = dict(side_effect=lambda n: usages[n])
    with patch.object(accounts, "usage_for", **common), \
         patch("ccswitch.ledger.get_status", return_value="ok"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.weekly_reserves", return_value={}), \
         patch("ccswitch.ledger.reserve"):
        assert accounts.pick_account_detailed(Config()).name == "hot"
    # keep 10% back: hot at 95% is over the 90 ceiling
    with patch.object(accounts, "usage_for", **common), \
         patch("ccswitch.ledger.get_status", return_value="ok"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.weekly_reserves", return_value={"hot": 10.0}), \
         patch("ccswitch.ledger.reserve"):
        assert accounts.pick_account_detailed(Config()).name == "cool"


# --- Caller exclusions are hard; quarantine/reservations stay relaxable -------
#
# Production incident 2026-09-08: sessions rotated tony_steinonline.net →
# tony_steinonline.net (same account) on reason=rate_limit, burned the
# 3-rotations-per-300s cap, and parked as `rotation_cap` — which
# `is_usage_stalled` renders as "parked for lack of usage headroom", while
# tony_kindash.com sat at 46% weekly. The account the caller was rotating AWAY
# from was under the 100% ceiling (Anthropic 429s before reported usage hits
# 100%, and the snapshot is up to 60s stale), so once the tiered fallback
# dropped `exclude` it was the best-scoring candidate and came straight back.

def _at_ceiling():
    from tests.helpers import usage
    return usage(session=100.0)


def _healthy():
    from tests.helpers import usage
    return usage(session=10.0, weekly=20.0)


def _patched_ledger(reserved=frozenset(), quarantined=frozenset()):
    """Ledger stubs: every account enabled and 'ok', none reserved."""
    return [
        patch("ccswitch.ledger.get_status", return_value="ok"),
        patch("ccswitch.ledger.is_account_enabled", return_value=True),
        patch("ccswitch.ledger.reservation_active",
              side_effect=lambda led, n, ts: n in reserved),
        patch("ccswitch.ledger.reserve"),
        patch("ccswitch.ledger.weekly_reserves", return_value={}),
        patch("ccswitch.ledger.extra_usage_allowed", return_value=frozenset()),
    ]


def _pick(monkeypatch, usages, **kwargs):
    import contextlib
    monkeypatch.setattr(accounts, "_account_names", lambda: list(usages))
    with contextlib.ExitStack() as stack:
        for p in _patched_ledger(reserved=kwargs.pop("reserved", frozenset())):
            stack.enter_context(p)
        stack.enter_context(patch.object(accounts, "usage_for",
                                         side_effect=lambda n: usages[n]))
        return accounts.pick_account_detailed(Config(), **kwargs)


def test_exclude_is_never_relaxed_back_onto_the_excluded_account(monkeypatch):
    """`exclude` is the caller's own knowledge ("this one just failed on me").
    It must survive every fallback tier, even when it leaves nothing pickable."""
    from ccswitch.selector import AllAccountsBusy
    import pytest
    usages = {"current": _healthy(), "other": _at_ceiling()}
    with pytest.raises(AllAccountsBusy) as ei:
        _pick(monkeypatch, usages, exclude={"current"})
    assert ei.value.earliest_reset is not None


def test_soft_exclude_is_relaxed_when_nothing_else_is_available(monkeypatch):
    """Quarantine IS relaxable — a rate-limited account may have reset since, and
    returning it beats starving the session. This is the tier the incident fix
    must preserve."""
    usages = {"quarantined": _healthy(), "other": _at_ceiling()}
    picked = _pick(monkeypatch, usages, soft_exclude={"quarantined"})
    assert picked.name == "quarantined"


def test_soft_exclude_still_loses_to_an_available_account(monkeypatch):
    """Relaxation is last-resort: a quarantined account is only picked when no
    un-quarantined account has headroom."""
    usages = {"quarantined": _healthy(), "free": _healthy()}
    picked = _pick(monkeypatch, usages, soft_exclude={"quarantined"})
    assert picked.name == "free"


def test_reservations_are_relaxed_but_exclude_still_is_not(monkeypatch):
    """A stale soft reservation must not starve the pick, yet relaxing it may
    never resurrect the excluded account."""
    usages = {"current": _healthy(), "reserved": _healthy()}
    picked = _pick(monkeypatch, usages, exclude={"current"},
                   reserved=frozenset({"reserved"}))
    assert picked.name == "reserved"


def test_a_growing_exclude_set_is_honoured_in_full(monkeypatch):
    """The ccrun/ccmax retry loops (`runner.py`, `launcher.py`) pass an
    accumulating `exclude=tried` set. Relaxing it re-picked an account already
    tried, so `len(tried)` stopped growing and the "all accounts tried" exit was
    never reached — the loop respawned on a failing account instead of raising
    AllAccountsBusy for the --wait branch to sleep on."""
    from ccswitch.selector import AllAccountsBusy
    import pytest
    usages = {"a": _healthy(), "b": _healthy(), "c": _at_ceiling()}
    assert _pick(monkeypatch, usages, exclude={"a"}).name == "b"
    with pytest.raises(AllAccountsBusy):
        _pick(monkeypatch, usages, exclude={"a", "b"})


def test_pick_account_detailed_fetches_usage_concurrently(monkeypatch):
    """The backlog runner's per-tick pick fetches every configured account's
    usage; done serially, a throttled/slow oauth usage endpoint compounds
    multiple ~15s retry budgets into one pick and stalls the daemon's event loop
    (#2223). Each account's ``usage_for`` must run on its own thread in
    parallel — proven with a barrier that only releases once every account's
    fetch has started, so a serial implementation deadlocks (and times out)
    instead of silently passing."""
    import contextlib
    import threading
    names = ["a", "b", "c"]
    monkeypatch.setattr(accounts, "_account_names", lambda: names)
    _patch_legacy_entries(monkeypatch)
    barrier = threading.Barrier(len(names), timeout=2.0)

    def slow_usage(n):
        barrier.wait()  # only returns once every account's fetch has started
        return _healthy()

    with contextlib.ExitStack() as stack:
        for p in _patched_ledger():
            stack.enter_context(p)
        stack.enter_context(patch.object(accounts, "usage_for", side_effect=slow_usage))
        picked = accounts.pick_account_detailed(Config())
    assert picked.name in names
