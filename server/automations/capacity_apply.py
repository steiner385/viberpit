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

#: How far into the future ``computedAt`` may read and still count as live —
#: ordinary clock skew between the writer and this reader, not a real window.
CLOCK_SKEW_TOLERANCE_S = 60


def plan_is_live(plan: dict | None, *, now: datetime, enabled: bool, max_age_s: int = PLAN_MAX_AGE_S) -> bool:
    """Whether ``plan`` should govern this tick: truthy, ``applied`` is exactly
    ``True``, the governor is enabled, and ``computedAt`` is no more than
    ``max_age_s`` in the past and no more than ``CLOCK_SKEW_TOLERANCE_S`` in the
    future of ``now`` — an ASYMMETRIC window, not a distance from ``now`` in
    either direction (see the ``age`` comment below). A naive ``computedAt`` is
    assumed UTC (the store always writes timezone-aware ISO strings; a naive one
    is defensive only). Never raises — an unparseable or wrong-shaped plan just
    reads as not live.
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
    # Freshness is a WINDOW, not a distance: `abs()` treated a plan computed in
    # the future (clock skew between the writer and this reader, or a corrupt
    # `computedAt`) as equally live as one that many minutes old, which is
    # backwards — a plan from the future was never actually computed against
    # the world this reader sees. `CLOCK_SKEW_TOLERANCE_S` absorbs ordinary
    # clock skew without opening the same hole `abs()` did.
    age = (at - computed).total_seconds()
    if age < -CLOCK_SKEW_TOLERANCE_S:
        # INFO, not WARNING: every read seam calls this every tick, and a WARNING
        # is an auto-error filing decision — a persistent skew would fan out.
        log.info("capacity_apply.plan_from_the_future: computedAt=%s is %.1fs ahead of now=%s "
                 "(tolerance %ss); plan is not live", computed_at, -age, at.isoformat(),
                 CLOCK_SKEW_TOLERANCE_S)
        return False
    return age <= max_age_s


def apply_product_plan(policies: Mapping[str, dict | None], plan: dict | None) -> dict[str, dict | None]:
    """Overlay the governor's per-product caps onto a slug -> policy mapping.

    Only a slug whose policy is stored as a dict (not ``None`` and not some
    other corrupt type, i.e. actually opted in), has an entry in
    ``plan["perProduct"]``, and reads ``capacity_mode == "auto"`` is overlaid —
    a pinned product's operator-set cap is never overridden, and a ``None`` or
    non-dict policy (never configured, or a corrupt stored row; dispatch is off
    regardless) is left unchanged rather than synthesized into a policy dict.
    Every overlay produces a NEW dict; inputs are never mutated. A malformed
    plan (wrong shape, a non-numeric cap) logs a warning and leaves the
    affected entr(y/ies) unchanged rather than raising.
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
        if isinstance(raw, dict) and slug in per_product and effective_policy(raw).get("capacity_mode") == "auto":
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
