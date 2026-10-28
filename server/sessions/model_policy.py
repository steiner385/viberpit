"""Model-approval policy — the single source of truth for whether a requested
model requires human approval, mirroring resolve_permission_mode's precedence.

    denylist (which tiers are gated):   AppSettings → env
    action  (what a gated tier does):   per-account ledger → AppSettings → env

Called at every point that materialises a model onto a session
(SessionOrchestrator.create_session and .set_model), so no create path can drift.
Those callers judge ``launch_model(requested)`` rather than the raw request: a
request that names no model still launches on one (the shared Claude settings
default), and that is the model the denylist has to see.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from sqlalchemy import select

from server.config import get_settings
from server.db.models import AppSettings
from server.sessions.engine import AgentEngine
from server.sessions.model_catalog import model_tier, normalized_alias

log = logging.getLogger("ccswitch.model_policy")

VALID_ACTIONS: frozenset[str] = frozenset({"allow", "require_approval", "block"})

# pending_model_reason for a hold whose model the requester never named.
INHERITED_DEFAULT_REASON = (
    "No model was requested; this is the shared Claude settings default the "
    "session would launch on.")


def _parse_denylist(raw: str | None) -> set[str]:
    return {t.strip().lower() for t in (raw or "").split(",") if t.strip()}


def _settings_model(path: Path) -> str | None:
    """The model *path* (a Claude settings file) pins: its ``env.ANTHROPIC_MODEL``,
    else its ``model`` key. None when the file is missing, unparseable, or pins
    nothing — fail-open to "no default", never to an exception: this runs on every
    session create and a torn read of the shared file must not strand a launch."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    env = data.get("env")
    for candidate in ((env.get("ANTHROPIC_MODEL") if isinstance(env, dict) else None),
                      data.get("model")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def inherited_default_model() -> str | None:
    """The model a Claude session launches on when the request names none.

    ViberPit passes no model to the ``claude`` binary for a "default" launch, so the
    binary picks one from the same sources it uses interactively, in its own
    precedence: ``ANTHROPIC_MODEL`` in its env (the spawn env is this process's
    environ), then settings.local.json, then settings.json. Every account's config
    dir symlinks those two files from ``paths.shared_base()``
    (``ccswitch.configdir.SHARE_ALLOWLIST``), so ONE default applies fleet-wide and
    is knowable before an account is picked — which is what lets the launch gate
    judge it. A ``/model`` pick in any session rewrites that shared key, so this is
    whatever the last interactive pick left behind, not a ViberPit setting.

    Returns None when nothing pins a model (the binary's own built-in default).
    """
    env_model = os.environ.get("ANTHROPIC_MODEL", "").strip()
    if env_model:
        return env_model
    from viberpit import paths
    base = paths.shared_base()
    for name in ("settings.local.json", "settings.json"):
        model = _settings_model(base / name)
        if model:
            return model
    return None


def launch_model(model: str | None, *,
                 engine: AgentEngine | str = AgentEngine.CLAUDE_CODE) -> str | None:
    """The model *model* would actually launch on, for the approval gate to judge.

    An explicit alias or pinned id is returned as-is. No model, or the picker's
    ``default`` row, resolves to ``inherited_default_model()`` for the Claude engine —
    the binary would silently launch on it, so the gate must see it. Non-Claude
    engines never inherit: Claude's settings.json says nothing about what a Codex
    thread runs on, and their own catalogs supply their defaults.
    """
    if str(getattr(engine, "value", engine)) != AgentEngine.CLAUDE_CODE.value:
        return model
    if normalized_alias(model) in (None, "default"):
        return inherited_default_model()
    return model


def quota_model(model: str | None, *,
                engine: AgentEngine | str = AgentEngine.CLAUDE_CODE) -> str | None:
    """The key quota windows are matched against for a session that runs *model*.

    The ONE place a session model becomes a ``QuotaWindow.applies_to_models`` key;
    every selection / rotation call site passes this and nothing re-derives it.

    Claude: the tier of the model the session will ACTUALLY launch on —
    ``launch_model`` resolves ``default``/None to the inherited settings model, so a
    "Default" session is judged on the fleet default the binary would pick. None when
    the tier is indeterminate (``best``, an unknown id, nothing pinned) or the engine
    is not Claude: the selector then applies EVERY window, which is the conservative
    direction (``QuotaWindow.applies_to``).
    """
    if str(getattr(engine, "value", engine)) != AgentEngine.CLAUDE_CODE.value:
        return None
    return model_tier(launch_model(model, engine=engine))


async def resolve_model_policy(db, *, model: str | None, account: str | None = None) -> str:
    """Return "allow" | "require_approval" | "block" for *model* on *account*.

    A denylist entry matches EITHER the model's tier ("fable" gates ``fable``,
    ``fable[1m]`` and ``claude-fable-5-1``) OR its bare alias ("best" gates ``best``).
    The alias arm exists because ``model_tier`` returns None for ``best`` / ``default``
    / unknown ids — and returning "allow" on a None tier let ``best`` bypass the gate
    entirely, even though it can resolve to the most expensive model available.
    """
    tier = model_tier(model)
    alias = normalized_alias(model)
    if alias is None:
        # No model requested at all — the session takes the account default, which is
        # not a selection to approve. Kept before the DB read: this is the hot path.
        return "allow"

    row = (await db.execute(
        select(AppSettings).where(AppSettings.id == "singleton"))).scalar_one_or_none()

    cfg = get_settings()
    denylist = _parse_denylist(
        (row.model_approval_denylist if row is not None else None)
        or cfg.model_approval_denylist)
    if tier not in denylist and alias not in denylist:
        return "allow"

    if account:
        from viberpit import ledger as _ledger
        # Read-only, off-loop: no flock, no fsync'd write-back on the event loop (#3150).
        led = await asyncio.to_thread(_ledger.read_ledger)
        acct_action = _ledger.get_account_defaults(led, account).get("modelApprovalAction")
        if acct_action in VALID_ACTIONS:
            return acct_action

    row_action = row.model_approval_action if row is not None else None
    if row_action in VALID_ACTIONS:
        return row_action
    env_action = cfg.model_approval_action
    return env_action if env_action in VALID_ACTIONS else "require_approval"


async def resolve_self_directed_model_policy(
    db, *, current_model: str | None, requested_model: str | None,
    account: str | None = None,
) -> str:
    """Decide what to do with a model the SESSION asked for itself.

    Returns "apply" | "park" | "refuse" | "noop".

    Composes two independent gates and takes the STRICTER outcome:

    1. The existing tier policy (``resolve_model_policy``) — block/require_approval
       apply to self-directed requests exactly as they apply to human ones. A blocked
       tier stays blocked even when the session is asking to move *down* to it.
    2. The direction rule — a cheaper tier auto-applies; a costlier one is parked for
       a human, because cost/quota risk is the axis worth a human's attention.

    Indeterminate direction (either side untiered: None/"default"/"best"/unknown id)
    parks. There is no honest cheaper/costlier comparison, and guessing one is how a
    session talks itself onto an expensive model.
    """
    from server.sessions.model_catalog import tier_rank

    tier_policy = await resolve_model_policy(db, model=requested_model, account=account)
    if tier_policy == "block":
        return "refuse"
    if tier_policy == "require_approval":
        return "park"

    current_rank = tier_rank(current_model)
    requested_rank = tier_rank(requested_model)
    if current_rank is None or requested_rank is None:
        return "park"
    if requested_rank == current_rank:
        return "noop"
    return "apply" if requested_rank < current_rank else "park"
