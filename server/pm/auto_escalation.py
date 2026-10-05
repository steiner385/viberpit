"""Auto-approving an approval-gated escalation within a budget (#2679).

When the model ladder stops below the operator's ``ladder_top`` because the
model-approval gate clamped it, the runner hands the issue to its owner with
"bounced at opus; fable needs approval" (``outcomes.escalation_for``). For whole
classes of issue — flaky-test trackers, deploy-verify alerts — that approval is a
rubber stamp. A product may name those classes (``auto_escalate_match``) and a
budget (``auto_escalate_budget`` per ``AUTO_ESCALATION_PERIOD``); the backlog tick
then approves such holds itself through the same path a person's tap takes
(``server/pm/escalation_approval.py``), and past the budget the hold waits for a
person exactly as before.

Pure: the orchestrator supplies the rows, the product policies and the count of
approvals already spent (``AUTO_APPROVED_AUDIT`` rows in the period).
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from server.pm.holds import RUNNER_ACTOR
from server.pm.outcomes import Escalation, escalation_for
from server.pm.queue import QueueRow

#: The rolling window ``auto_escalate_budget`` counts over.
AUTO_ESCALATION_PERIOD = timedelta(days=7)
#: The audit row each automatic approval writes — the budget's ledger, and what
#: tells an automatic approval apart from a person's ``pm.hold.escalation_approved``.
AUTO_APPROVED_AUDIT = "pm.hold.escalation_auto_approved"


def matches(policy: dict, *, title: str, labels: Iterable[str]) -> str | None:
    """The ``auto_escalate_match`` entry that covers an issue, or ``None``. An entry
    covers an issue carrying it as a label, or whose title contains it — both
    case-insensitive, so ``flaky`` matches the ``flaky`` label and ``[flaky]``
    matches the ``[flaky] …`` title prefix."""
    lowered_labels = {str(lbl).strip().lower() for lbl in labels}
    lowered_title = (title or "").lower()
    for entry in policy.get("auto_escalate_match") or ():
        needle = str(entry).strip().lower()
        if needle and (needle in lowered_labels or needle in lowered_title):
            return str(entry)
    return None


def budget(policy: dict) -> int:
    return max(0, int(policy.get("auto_escalate_budget") or 0))


def candidates(rows: Iterable[QueueRow], policies: dict[str, dict],
               used: dict[str, int]) -> list[tuple[QueueRow, Escalation, str]]:
    """The held rows the tick may approve now, oldest hold first, as
    ``(row, escalation, matched_entry)``: an approval-gated hand-off the RUNNER
    wrote (a person's own ``owner_action`` hold is theirs, whatever it says), on a
    product whose policy covers the issue, while that product's budget lasts.
    *used* is approvals already spent per product slug in the period."""
    eligible: list[tuple[QueueRow, Escalation, str]] = []
    for row in rows:
        if row.hold_by != RUNNER_ACTOR:
            continue
        escalation = escalation_for(row.hold_kind, row.hold_reason)
        if escalation is None:
            continue
        entry = matches(policies.get(row.product) or {}, title=row.title, labels=row.labels)
        if entry is not None:
            eligible.append((row, escalation, entry))
    eligible.sort(key=lambda e: (e[0].hold_set_at is None,
                                 e[0].hold_set_at or datetime.min, e[0].ref))
    spent = dict(used)
    out: list[tuple[QueueRow, Escalation, str]] = []
    for row, escalation, entry in eligible:
        if spent.get(row.product, 0) >= budget(policies.get(row.product) or {}):
            continue
        spent[row.product] = spent.get(row.product, 0) + 1
        out.append((row, escalation, entry))
    return out
