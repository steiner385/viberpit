"""Tests for server.sessions.selection (T020).

Uses temp_env() from tests.helpers for store isolation and monkeypatches
ccswitch.accounts.usage_for so no real network calls are made.

Assertions:
- Two sequential select_account() calls return DIFFERENT accounts
  (the soft reservation prevents double-assignment).
- Neither call returns an account at or above the usage ceiling.
- AllAccountsBusy (with .earliest_reset) is raised when all accounts
  are at ceiling.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from tests.helpers import temp_env
from ccswitch.oauth import Usage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_account(name: str) -> None:
    """Write a minimal credential slot so pick_account can find the account."""
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    now_ms = int(time.time() * 1000)
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok",
        "refreshToken": f"{name}-ref",
        "expiresAt": now_ms + 9_000_000,
        "subscriptionType": "max",
    })
    # shared_mcp_path must also exist
    shared_mcp = paths.shared_mcp_path()
    if not shared_mcp.exists():
        write_json_secure(shared_mcp, {})


def _ceiling_usage() -> Usage:
    """Usage at 100% session — treated as at-ceiling by select_best."""
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return Usage(100.0, reset, 20.0, reset, False)


def _headroom_usage(session_pct: float = 10.0) -> Usage:
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return Usage(session_pct, reset, 20.0, reset, False)


# ---------------------------------------------------------------------------
# T020-a: two sequential calls return different accounts (reservation)
# ---------------------------------------------------------------------------

def test_sequential_calls_return_different_accounts():
    """After the first selection, the winner is soft-reserved; the second call
    must pick a different account."""
    with temp_env():
        _seed_account("alpha")
        _seed_account("beta")
        _seed_account("gamma")

        # All three have headroom — different utilizations so the order is deterministic
        usage_map = {
            "alpha": _headroom_usage(session_pct=30.0),
            "beta":  _headroom_usage(session_pct=10.0),  # lowest → picked first
            "gamma": _headroom_usage(session_pct=50.0),
        }

        def fake_usage(name: str) -> Usage:
            return usage_map[name]

        with patch("ccswitch.accounts.usage_for", side_effect=fake_usage):
            from server.sessions.selection import select_account

            first = select_account()
            second = select_account()

        assert first != second, f"expected different accounts, got {first!r} twice"


# ---------------------------------------------------------------------------
# T020-b: accounts at ceiling are never selected
# ---------------------------------------------------------------------------

def test_ceiling_account_never_selected():
    """An account with session_pct=100 must never be returned."""
    with temp_env():
        _seed_account("limited")
        _seed_account("alpha")

        usage_map = {
            "limited": _ceiling_usage(),
            "alpha": _headroom_usage(session_pct=20.0),
        }

        def fake_usage(name: str) -> Usage:
            return usage_map[name]

        with patch("ccswitch.accounts.usage_for", side_effect=fake_usage):
            from server.sessions.selection import select_account

            result = select_account()

        assert result == "alpha", f"expected 'alpha', got {result!r}"


# ---------------------------------------------------------------------------
# T020-c: AllAccountsBusy raised when all at ceiling
# ---------------------------------------------------------------------------

def test_all_at_ceiling_raises_all_accounts_busy():
    """When every account is at/above ceiling, AllAccountsBusy is raised
    and carries an earliest_reset datetime."""
    with temp_env():
        _seed_account("alpha")
        _seed_account("beta")

        reset = datetime.now(timezone.utc) + timedelta(hours=3)

        def fake_usage(name: str) -> Usage:
            return Usage(100.0, reset, 20.0, reset, False)

        with patch("ccswitch.accounts.usage_for", side_effect=fake_usage):
            from server.sessions.selection import select_account, AllAccountsBusy

            with pytest.raises(AllAccountsBusy) as exc_info:
                select_account()

        assert exc_info.value.earliest_reset is not None


# ---------------------------------------------------------------------------
# T020-d: exclude parameter prevents selecting the excluded account
# ---------------------------------------------------------------------------

def test_exclude_prevents_current_account_selection():
    """Passing exclude={'beta'} ensures 'beta' is never returned."""
    with temp_env():
        _seed_account("alpha")
        _seed_account("beta")

        usage_map = {
            "alpha": _headroom_usage(session_pct=50.0),
            "beta":  _headroom_usage(session_pct=10.0),  # best, but excluded
        }

        def fake_usage(name: str) -> Usage:
            return usage_map[name]

        with patch("ccswitch.accounts.usage_for", side_effect=fake_usage):
            from server.sessions.selection import select_account

            result = select_account(exclude=frozenset({"beta"}))

        assert result == "alpha"
