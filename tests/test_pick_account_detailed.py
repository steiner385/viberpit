from unittest.mock import patch
from ccswitch.config import Config
from ccswitch.selector import Picked
from ccswitch import accounts


def test_pick_account_detailed_optimistic_when_usage_none(monkeypatch):
    monkeypatch.setattr(accounts, "_account_names", lambda: ["a", "b"])
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
    with patch.object(accounts, "usage_for", return_value=None), \
         patch("ccswitch.ledger.get_status", return_value="needs-reauth"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.reserve"):
        with pytest.raises(AllAccountsBusy) as ei:
            accounts.pick_account_detailed(Config())
    assert ei.value.kind == "no_usable_accounts"


def test_pick_account_str_wrapper_returns_name(monkeypatch):
    monkeypatch.setattr(accounts, "_account_names", lambda: ["a"])
    with patch.object(accounts, "usage_for", return_value=None), \
         patch("ccswitch.ledger.get_status", return_value="ok"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.reserve"):
        assert accounts.pick_account(Config()) == "a"


def test_pick_account_detailed_reserve_false_does_not_reserve(monkeypatch):
    """A read-only availability probe (reserve=False) must NOT write a reservation —
    reserving on a check starves real spawns/creates when only one account has room."""
    monkeypatch.setattr(accounts, "_account_names", lambda: ["a"])
    with patch.object(accounts, "usage_for", return_value=None), \
         patch("ccswitch.ledger.get_status", return_value="ok"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.reserve") as reserve:
        picked = accounts.pick_account_detailed(Config(), reserve=False)
    assert picked == Picked(name="a", optimistic=True)
    reserve.assert_not_called()


def test_pick_account_detailed_reserve_true_still_reserves(monkeypatch):
    monkeypatch.setattr(accounts, "_account_names", lambda: ["a"])
    with patch.object(accounts, "usage_for", return_value=None), \
         patch("ccswitch.ledger.get_status", return_value="ok"), \
         patch("ccswitch.ledger.reservation_active", return_value=False), \
         patch("ccswitch.ledger.reserve") as reserve:
        accounts.pick_account_detailed(Config())  # reserve defaults to True
    reserve.assert_called_once()
