"""Account selection for session creation.

Thin wrapper around ``ccswitch.accounts.pick_account_detailed`` that:
- Loads the current ``Config`` on each call (so tests can mutate env between calls).
- Re-raises ``ccswitch.selector.AllAccountsBusy`` with its ``.earliest_reset``.
- Accepts a hard ``exclude`` frozenset (rotation's current account) and a
  relaxable ``soft_exclude`` frozenset (quarantine).
"""
from __future__ import annotations

from ccswitch.accounts import pick_account_detailed
from ccswitch.config import load_config
from ccswitch.selector import AllAccountsBusy, Picked  # re-exported so callers only import from here
from server.sessions.engine import AgentEngine


__all__ = ["select_account", "AllAccountsBusy", "Picked"]


def select_account(exclude: frozenset[str] = frozenset(),
                   load: dict[str, float] | None = None,
                   *, reserve: bool = True,
                   soft_exclude: frozenset[str] = frozenset(),
                   engine: AgentEngine = AgentEngine.CLAUDE_CODE) -> Picked:
    """Select the best available account, skipping any in *exclude*.

    *exclude* is HARD: no fallback tier may hand back an account listed there.
    Rotation puts the account it is moving away from here, so a session can never
    be "rotated" onto the account that just failed it. *soft_exclude* is the
    relaxable hint (quarantine), dropped only when nothing else has headroom.
    See ``pick_account_detailed`` for why the two must not be merged.

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
    return pick_account_detailed(
        cfg,
        exclude=set(exclude),
        load=load,
        reserve=reserve,
        soft_exclude=set(soft_exclude),
        require_engine=engine.value,
        default_engine=AgentEngine.CLAUDE_CODE.value,
    )
