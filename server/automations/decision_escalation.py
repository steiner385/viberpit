"""Decision Escalation (spec 2026-09-30 §5): one digest per product when its
decisions are starving dispatch, ageing, or blocking a lot of work.

Pure half first — ``decide``/``fingerprint``/``should_send``/``digest_text`` take
the read model (``server/pm/escalation.py``) and answer without I/O — then the
automation class, the loop and the action provider, in the ``DeployMonitor``
mould (``server/automations/deploy_monitor.py``).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from server.pm.escalation import ProductEscalation

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
    or the last digest is older than ``repeat_hours``)."""
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
