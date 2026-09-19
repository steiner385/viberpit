"""Overlay the capacity governor's plan onto product policy / fleet config
(spec 2026-09-19 §... phase 2). Pure: no I/O, no DB, no orchestrator, no
``server.config`` import — every read seam (queue loader, dispatch tap, daemon
capacity) calls these with values it already loaded.

``plan_is_live`` is the ONE liveness check: a caller applies the governor's plan
only when it says yes, so the queue, the tap and the daemon capacity check cannot
disagree about whether a stale or disabled plan still governs a tick. The two
``apply_*`` functions never raise on a malformed plan — a corrupt or partially
written row must degrade to "no change" (the pre-governor policy/config), the
same posture ``product_policy.effective_policy`` takes for a corrupt policy row.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from server.automations.product_policy import effective_policy

log = logging.getLogger(__name__)

#: A plan older than this (relative to ``now``) is not "live" even if ``applied``
#: is still true — the governor loop stalled or died without clearing the flag.
PLAN_MAX_AGE_S = 600


def plan_is_live(plan: dict | None, *, now: datetime, enabled: bool, max_age_s: int = PLAN_MAX_AGE_S) -> bool:
    """Whether ``plan`` should govern this tick: truthy, ``applied`` is exactly
    ``True``, the governor is enabled, and ``computedAt`` parses to within
    ``max_age_s`` of ``now``. A naive ``computedAt`` is assumed UTC (the store
    always writes timezone-aware ISO strings; a naive one is defensive only).
    Never raises — an unparseable or wrong-shaped plan just reads as not live.
    """
    if not enabled or not plan:
        return False
    try:
        if plan.get("applied") is not True:
            return False
        computed_at = plan.get("computedAt")
    except AttributeError:
        return False
    if not computed_at:
        return False
    try:
        computed = datetime.fromisoformat(computed_at)
    except (TypeError, ValueError):
        return False
    if computed.tzinfo is None:
        computed = computed.replace(tzinfo=timezone.utc)
    at = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return abs((at - computed).total_seconds()) <= max_age_s


def apply_product_plan(policies: Mapping[str, dict | None], plan: dict | None) -> dict[str, dict | None]:
    """Overlay the governor's per-product caps onto a slug -> policy mapping.

    Only a slug whose policy is stored (not ``None``, i.e. actually opted in),
    has an entry in ``plan["perProduct"]``, and reads ``capacity_mode == "auto"``
    is overlaid — a pinned product's operator-set cap is never overridden, and a
    ``None`` policy (never configured; dispatch is off regardless) is left
    ``None`` rather than synthesized into a policy dict. Every overlay produces a
    NEW dict; inputs are never mutated. A malformed plan (wrong shape, a
    non-numeric cap) logs a warning and leaves the affected entr(y/ies)
    unchanged rather than raising.
    """
    if not plan:
        return dict(policies)
    try:
        per_product = plan.get("perProduct") or {}
        list(per_product.items())
    except (AttributeError, TypeError):
        log.warning("capacity_apply.malformed_plan: perProduct is not a mapping, no change")
        return dict(policies)
    out: dict[str, dict | None] = {}
    for slug, raw in policies.items():
        if raw is not None and slug in per_product and effective_policy(raw).get("capacity_mode") == "auto":
            try:
                cap = int(per_product[slug])
            except (TypeError, ValueError):
                log.warning("capacity_apply.malformed_plan: perProduct[%s]=%r is not an int, no change",
                            slug, per_product[slug])
                out[slug] = raw
                continue
            out[slug] = {**raw, "max_in_flight": cap}
        else:
            out[slug] = raw
    return out


def apply_fleet_plan(config: Mapping[str, Any], plan: dict | None) -> dict:
    """Cap ``config["max_concurrent"]`` at the governor's fleet concurrency,
    whichever is lower. A plan with no ``fleetConcurrency`` (or a malformed one)
    is "no change": returns a copy of ``config``, logging a warning for the
    malformed case. Never mutates ``config``.
    """
    if not plan:
        return dict(config)
    try:
        fleet_concurrency = plan.get("fleetConcurrency")
    except AttributeError:
        log.warning("capacity_apply.malformed_plan: plan is not a mapping, no change")
        return dict(config)
    if fleet_concurrency is None:
        return dict(config)
    try:
        fleet_cap = int(fleet_concurrency)
        current = int(config["max_concurrent"])
    except (TypeError, ValueError, KeyError):
        log.warning("capacity_apply.malformed_plan: fleetConcurrency=%r or max_concurrent unusable, no change",
                    fleet_concurrency)
        return dict(config)
    return {**config, "max_concurrent": min(current, fleet_cap)}
