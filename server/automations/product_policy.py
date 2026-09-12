"""Product automation policy (spec 2026-09-12-product-automation-policy-design §4): the ONE
schema (`PRODUCT_POLICY_SCHEMA`) and the ONE validated reading (`effective_policy`) for every
product's automation knobs — whether the Backlog Runner dispatches for it, how far down its
board it reaches, its in-flight cap and weight, its attempt cap, its session model.

Phase 1 only: `triage_auto_accept` (phase 2) and `give_up_after_bounces` (phase 3) are not part
of this schema yet.

Stored on ``Project.automation_policy`` as a JSON blob validated through this schema at write
time (``PATCH /api/pm/projects/{slug}/policy``) — so a row in the DB is already valid. But
``effective_policy`` reads it again on every use (never trusts the write-time validation alone)
because a hand-edited row or a shipped schema change that forgot to retire an old key must never
take a product's dispatch decision down with it: every reader falls back to pure defaults
(dispatch ``off``, the safe "not opted in" state) rather than raising.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from server.automations.base import ConfigField, validate_config
from server.automations.model_pick import AUTOMATION_MODEL_CHOICES
from server.db.models import Project
from server.pm.desired_state import STATUS_READY
from server.pm.queue import MIN_STATUS_OPTIONS

log = logging.getLogger(__name__)

DISPATCH_MODES: tuple[str, ...] = ("off", "propose", "auto")

PRODUCT_POLICY_SCHEMA: list[ConfigField] = [
    ConfigField(key="dispatch", label="Dispatch", type="enum", default="off",
                choices=list(DISPATCH_MODES),
                help="Whether the Backlog Runner works this product's board: 'propose' "
                     "announces a batch without spawning, 'auto' spawns."),
    ConfigField(key="queue_min_status", label="Minimum board status", type="enum",
                default=STATUS_READY, choices=list(MIN_STATUS_OPTIONS),
                help="How far down the board the runner may reach for this product."),
    ConfigField(key="max_in_flight", label="Max in flight", type="int", default=1,
                help="This product's concurrent runner sessions."),
    ConfigField(key="weight", label="Weight", type="int", default=1,
                help="This product's share when a freed slot is contested (the "
                     "'weighted' strategy)."),
    ConfigField(key="max_attempts", label="Max attempts per issue", type="int", default=2),
    ConfigField(key="default_model", label="Session model", type="enum", default="auto",
                choices=AUTOMATION_MODEL_CHOICES,
                help="Model for this product's sessions; 'auto' assesses each job."),
    ConfigField(key="respect_dependencies", label="Respect issue dependencies", type="bool",
                default=True),
]

# Logged once per process, not once per bad row: a corrupt policy is a per-deploy fact
# ("something upstream needs fixing"), not a per-request one, and every dispatch-loop tick
# would otherwise re-log the same row.
_warned_bad_policy = False


def effective_policy(raw: dict | None) -> dict:
    """The validated policy with every key present. ``None`` (never opted in) and an
    unparseable blob both read as the defaults — dispatch ``off`` — and the latter logs once
    per process. Never raises."""
    global _warned_bad_policy
    if raw is not None:
        try:
            return validate_config(PRODUCT_POLICY_SCHEMA, raw)
        except (ValueError, TypeError):
            if not _warned_bad_policy:
                log.warning("product_policy.invalid_stored_policy: falling back to defaults",
                            exc_info=True)
                _warned_bad_policy = True
    return validate_config(PRODUCT_POLICY_SCHEMA, {})


def policies_by_slug(db: Session) -> dict[str, dict]:
    """slug -> effective policy for every Project (sync session; the queue loader's session)."""
    rows = db.execute(select(Project.slug, Project.automation_policy)).all()
    return {slug: effective_policy(policy) for slug, policy in rows if slug is not None}


async def load_policies(db: AsyncSession) -> dict[str, dict]:
    """Async twin of ``policies_by_slug`` for the API/orchestrator paths — same reading, no
    second rule."""
    rows = (await db.execute(select(Project.slug, Project.automation_policy))).all()
    return {slug: effective_policy(policy) for slug, policy in rows if slug is not None}
