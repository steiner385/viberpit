"""Model-approval policy — the single source of truth for whether a requested
model requires human approval, mirroring resolve_permission_mode's precedence.

    denylist (which tiers are gated):   AppSettings → env
    action  (what a gated tier does):   per-account ledger → AppSettings → env

Called at every point that materialises a model onto a session
(SessionOrchestrator.create_session and .set_model), so no create path can drift.
"""
from __future__ import annotations

from sqlalchemy import select

from server.config import get_settings
from server.db.models import AppSettings
from server.sessions.model_catalog import model_tier

VALID_ACTIONS: frozenset[str] = frozenset({"allow", "require_approval", "block"})


def _parse_denylist(raw: str | None) -> set[str]:
    return {t.strip().lower() for t in (raw or "").split(",") if t.strip()}


async def resolve_model_policy(db, *, model: str | None, account: str | None = None) -> str:
    """Return "allow" | "require_approval" | "block" for *model* on *account*."""
    tier = model_tier(model)
    if tier is None:
        return "allow"

    row = (await db.execute(
        select(AppSettings).where(AppSettings.id == "singleton"))).scalar_one_or_none()

    cfg = get_settings()
    denylist = _parse_denylist(
        (row.model_approval_denylist if row is not None else None)
        or cfg.model_approval_denylist)
    if tier not in denylist:
        return "allow"

    if account:
        from ccswitch import ledger as _ledger
        with _ledger.locked_ledger() as led:
            acct_action = _ledger.get_account_defaults(led, account).get("modelApprovalAction")
        if acct_action in VALID_ACTIONS:
            return acct_action

    row_action = row.model_approval_action if row is not None else None
    if row_action in VALID_ACTIONS:
        return row_action
    env_action = cfg.model_approval_action
    return env_action if env_action in VALID_ACTIONS else "require_approval"
