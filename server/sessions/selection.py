"""Account selection for session creation.

Thin wrapper around ``ccswitch.accounts.pick_account_detailed`` that:
- Loads the current ``Config`` on each call (so tests can mutate env between calls).
- Re-raises ``ccswitch.selector.AllAccountsBusy`` with its ``.earliest_reset``.
- Accepts an ``exclude`` frozenset so rotation can skip the current account.
"""
from __future__ import annotations

from ccswitch.accounts import pick_account_detailed
from ccswitch.config import load_config
from ccswitch.selector import AllAccountsBusy, Picked  # re-exported so callers only import from here


__all__ = ["select_account", "AllAccountsBusy", "Picked"]


def select_account(exclude: frozenset[str] = frozenset(),
                   load: dict[str, float] | None = None,
                   *, reserve: bool = True) -> Picked:
    """Select the best available account, skipping any in *exclude*.

    When ``reserve`` is True (default), writes a soft reservation into the ledger
    so concurrent calls don't double-assign the same account (R8 / FR-009a). A
    read-only availability probe MUST pass ``reserve=False`` — otherwise the check
    burns the account's reservation window without starting a session, starving
    real work when only one account has headroom. *load* is an optional
    per-account weighted live-session count; accounts already carrying load are
    discounted so new sessions spread out (avoids 429s).

    Raises:
        AllAccountsBusy: when every account is at or above the usage ceiling.
            The exception carries ``.earliest_reset`` (datetime | None).
    """
    cfg = load_config()
    return pick_account_detailed(cfg, exclude=set(exclude), load=load, reserve=reserve)
