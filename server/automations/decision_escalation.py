"""Decision Escalation (spec 2026-09-30 §5): one digest per product when its
decisions are starving dispatch, ageing, or blocking a lot of work.

Pure half first — ``decide``/``fingerprint``/``should_send``/``digest_text`` take
the read model (``server/pm/escalation.py``) and answer without I/O — then the
automation class, the loop and the action provider, in the ``DeployMonitor``
mould (``server/automations/deploy_monitor.py``).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from server.automations.base import ActionableItem, ActionSpec, AutomationStatus, ConfigField
from server.automations.context import observation_failed, publish_observation
from server.automations.engine_policy import EnginePolicy
from server.pm.escalation import HeldItem, ProductEscalation, rank_held
from server.sessions.notify_rules import in_quiet_hours

log = logging.getLogger(__name__)

AUTOMATION_ID = "decision-escalation"
EVENT_CLASS = "decision_needed"
#: The dedup record: one audit row per digest sent, read back on the next tick.
DIGEST_AUDIT_TYPE = "pm.escalation.digest"
REASONS = ("starving", "aged", "downstream")


@dataclass(frozen=True)
class Thresholds:
    starving_max: int = 1
    hold_age_hours: float = 48.0
    downstream_threshold: int = 3
    repeat_hours: float = 24.0


@dataclass(frozen=True)
class Verdict:
    slug: str
    escalate: bool
    reasons: tuple[str, ...]
    fingerprint: str


def fingerprint(esc: ProductEscalation) -> str:
    """What "the situation changed" means: the held SET, starvation, and the
    loudest item's reach. Age is deliberately not in it — time passing is what
    ``repeat_hours`` handles, not a new situation."""
    payload = {"slug": esc.slug, "starving": esc.starving,
               "held": sorted(i.ref for i in esc.held),
               "max_downstream": max((i.downstream for i in esc.held), default=0)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def decide(esc: ProductEscalation, t: Thresholds) -> Verdict:
    reasons: list[str] = []
    if esc.held:
        if esc.starving:
            reasons.append("starving")
        if esc.oldest_hold_hours is not None and esc.oldest_hold_hours >= t.hold_age_hours:
            reasons.append("aged")
        if any(i.downstream >= t.downstream_threshold for i in esc.held):
            reasons.append("downstream")
    return Verdict(slug=esc.slug, escalate=bool(reasons), reasons=tuple(reasons),
                   fingerprint=fingerprint(esc))


def should_send(verdict: Verdict, last: tuple[str, datetime] | None, *, now: datetime,
                t: Thresholds) -> bool:
    """Send when there is a verdict AND (nothing sent before, the situation changed,
    or the last digest is older than ``repeat_hours``).

    ``now`` and ``last[1]`` are both naive UTC (the audit row's ``at`` is naive)."""
    if not verdict.escalate:
        return False
    if last is None:
        return True
    last_fp, last_at = last
    if last_fp != verdict.fingerprint:
        return True
    return now - last_at >= timedelta(hours=t.repeat_hours)


def _age_label(hours: float | None) -> str:
    if hours is None:
        return "?"
    return f"{int(hours // 24)}d" if hours >= 48 else f"{int(hours)}h"


def digest_text(esc: ProductEscalation, verdict: Verdict, *, top: int = 3) -> tuple[str, str]:
    decisions = sum(1 for i in esc.held if i.hold_kind == "decision")
    title = (f"{esc.slug}: {len(esc.held)} held ({decisions} decision"
             f"{'' if decisions == 1 else 's'}), {esc.dispatchable} dispatchable")
    lines: list[str] = []
    for n, item in enumerate(esc.held[:top], start=1):
        rec = f" (recommended: {item.recommended_key})" if item.recommended_key else ""
        lines.append(f"#{n} {item.title} — blocks {item.downstream}{rec}")
    rest = len(esc.held) - min(top, len(esc.held))
    tail = [f"{rest} more"] if rest else []
    tail.append(f"oldest {_age_label(esc.oldest_hold_hours)}")
    tail.append("why: " + ", ".join(verdict.reasons))
    lines.append(" · ".join(tail))
    return title, "\n".join(lines)


class DecisionEscalation:
    id = AUTOMATION_ID
    name = "Decision Escalation"
    work_class = "maintenance"
    description = ("Watches every product's held items. When decisions are starving the "
                   "runner, ageing, or blocking a lot of work, sends one digest per product "
                   "through the notification rules — never a ping per item, never in quiet "
                   "hours. Never resolves a decision itself.")
    modes: tuple[str, ...] = ("off", "notify")
    engine_policy: EnginePolicy = EnginePolicy.CLAUDE_ONLY
    config_schema = [
        ConfigField(key="tick_min", label="Check every (minutes)", type="int", default=30, min=5),
        ConfigField(key="starving_max_dispatchable", label="Starving at or below (dispatchable)",
                    type="int", default=1, min=0,
                    help="A product with this many or fewer dispatchable items while anything "
                         "is held is starving."),
        ConfigField(key="hold_age_hours", label="Escalate a hold older than (hours)", type="int",
                    default=48, min=1),
        ConfigField(key="downstream_threshold", label="Escalate when a hold blocks at least",
                    type="int", default=3, min=1),
        ConfigField(key="repeat_hours", label="Repeat an unchanged digest after (hours)",
                    type="int", default=24, min=1),
        ConfigField(key="products", label="Products", type="string-list", default=[],
                    help="Limit to these product slugs (empty = every product with dispatch on)."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "decision_escalation_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        products = list(getattr(ctx, "decision_escalation_products", None) or [])
        held, starving = int(counts.get("held", 0)), int(counts.get("starving_products", 0))
        n = len(products)
        text = (f"{n} product{'' if n == 1 else 's'} · {held} held · "
                f"{starving} starving")
        level = "warn" if starving else ("active" if held else "idle")
        return AutomationStatus(text=text, level=level, counts=dict(counts), products=products)

    async def on_state_change(self, *, enabled: bool, mode: str | None, config: dict,
                              ctx: Any) -> None:
        ...  # the loop reads mode/config each tick


def thresholds_from(config: dict) -> Thresholds:
    return Thresholds(starving_max=int(config.get("starving_max_dispatchable", 1)),
                      hold_age_hours=float(config.get("hold_age_hours", 48)),
                      downstream_threshold=int(config.get("downstream_threshold", 3)),
                      repeat_hours=float(config.get("repeat_hours", 24)))


def _item_url(item: HeldItem) -> str:
    if item.hold_kind == "decision" and item.proposal_id:
        return f"/roadmap?scope=inbox&proposal={item.proposal_id}"
    return f"/roadmap?scope=inbox&product={item.product}"


def actionable_from(held: list[HeldItem], *, limit: int = 50) -> list[ActionableItem]:
    """The ranked held list as Needs-you rows (decision 7): one tap, by a person."""
    items: list[ActionableItem] = []
    for item in held[:limit]:
        if item.hold_kind == "decision" and item.proposal_id and item.recommended_key:
            actions = [ActionSpec(kind="accept_recommended", label="Accept recommendation")]
        elif item.hold_kind == "owner_action":
            actions = [ActionSpec(kind="retry", label="Retry")]
        else:
            actions = []
        subtitle = (f"{item.hold_kind.replace('_', ' ')} · blocks {item.downstream} · "
                    f"{_age_label(item.age_hours)} · {item.released_by}")
        items.append(ActionableItem(id=item.ref, title=item.title or item.ref, subtitle=subtitle,
                                    url=_item_url(item), actions=actions))
    return items


class DecisionEscalationLoop:
    def __init__(self, ctx: Any, *, read_config, load_escalations, quiet_window, last_digest,
                 record_digest, notify, now=None) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._load_escalations = load_escalations
        self._quiet_window = quiet_window
        self._last_digest = last_digest
        self._record_digest = record_digest
        self._notify = notify
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()
        self._held: list[HeldItem] = []

    async def tick(self) -> None:
        async with self._lock:
            try:
                await self._run_once()
            except Exception:
                observation_failed(self._ctx)
                log.warning("decision_escalation.tick_failed", exc_info=True)

    async def _run_once(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            return
        now = self._now()
        start, end = await self._quiet_window()
        if in_quiet_hours(start, end, now):
            # Decision 4: defer, don't store — the next tick outside the window
            # re-evaluates from live state, so nothing is lost and nothing leaks.
            return
        t = thresholds_from(config)
        escs = await self._load_escalations(config)
        held_all: list[HeldItem] = []
        products: list[dict] = []
        for esc in escs:
            held_all.extend(esc.held)
            products.append({"slug": esc.slug, "dispatchable": esc.dispatchable,
                             "held": len(esc.held), "starving": esc.starving,
                             "oldestHoldHours": esc.oldest_hold_hours})
            verdict = decide(esc, t)
            last = await self._last_digest(esc.slug)
            if not should_send(verdict, last, now=now.replace(tzinfo=None), t=t):
                continue
            title, body = digest_text(esc, verdict)
            await self._notify(title, body, repo=None,
                               url=f"/roadmap?scope=inbox&product={esc.slug}")
            await self._record_digest(esc.slug, {
                "slug": esc.slug, "fingerprint": verdict.fingerprint,
                "heldCount": len(esc.held), "dispatchable": esc.dispatchable,
                "starving": esc.starving, "reasons": list(verdict.reasons),
                "topRefs": [i.ref for i in esc.held[:3]]})
        self._held = rank_held(held_all)
        publish_observation(self._ctx, "decision_escalation_counts", {
            "held": len(held_all),
            "decisions": sum(1 for i in held_all if i.hold_kind == "decision"),
            "starving_products": sum(1 for p in products if p["starving"]),
            "dispatchable": sum(int(p["dispatchable"]) for p in products)})
        setattr(self._ctx, "decision_escalation_products", products)

    async def actionable_items(self, limit: int = 50) -> list[ActionableItem]:
        return actionable_from(self._held, limit=limit)
