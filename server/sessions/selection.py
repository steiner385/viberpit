"""Account selection for session creation.

Thin wrapper around ``ccswitch.accounts.pick_account`` that:
- Loads the current ``Config`` on each call (so tests can mutate env between calls).
- Re-raises ``ccswitch.selector.AllAccountsBusy`` with its ``.earliest_reset``.
- Accepts an ``exclude`` frozenset so rotation can skip the current account.
"""
from __future__ import annotations

from ccswitch.accounts import pick_account
from ccswitch.config import load_config
from ccswitch.selector import AllAccountsBusy  # re-exported so callers only import from here


__all__ = ["select_account", "AllAccountsBusy"]


def select_account(exclude: frozenset[str] = frozenset()) -> str:
    """Select the best available account, skipping any in *exclude*.

    Writes a soft reservation into the ledger so concurrent calls don't
    double-assign the same account (R8 / FR-009a).

    Raises:
        AllAccountsBusy: when every account is at or above the usage ceiling.
            The exception carries ``.earliest_reset`` (datetime | None).
    """
    cfg = load_config()
    # pick_account already calls select_best + ledger.reserve under a lock.
    return pick_account(cfg, exclude=set(exclude))
