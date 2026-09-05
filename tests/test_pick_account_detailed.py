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
    with patch.object(accounts, "usage_for", **common), \
         patch("ccswitch.ledger.get_status", return_value="ok"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.weekly_reserves", return_value={"hot": 90.0}), \
         patch("ccswitch.ledger.reserve"):
        assert accounts.pick_account_detailed(Config()).name == "cool"
