"""THE definition of "needs you" vs "waiting" (design 2026-10-14 §1).

An item **needs you** when only a human can unblock it: a ``HELD_KINDS`` hold, a
session waiting on a person, a bounced-out issue that needs a better spec, an
initiative gate, a triage proposal — or an open issue assigned on GitHub to the
product owner (``assigned_to_owner``). ``server/pm/tree.py:issue_tier`` reads
that half.

Everything else that is not moving is **waiting**, with a reason. The reasons
come from the queue's own skip verdicts (``server/pm/queue.py:skip_reason``), so
the map's waiting counters and ``GET /api/pm/queue`` cannot disagree about why
an item is not moving. ``waiting_reason_for_skip`` is the one mapping from a
skip reason to a waiting bucket; ``WAITING_KEYS`` is the served (camelCase)
bucket set in display order.

Pure: no I/O.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from server.automations.product_policy import trusted_authors
from server.pm.desired_state import (HELD_KINDS, STATUS_DONE, STATUS_HELD,
                                     STATUS_IN_PROGRESS)
from server.pm.queue import QueueRow, is_automation_author

#: Every waiting bucket, in the stable order the map serves and the web shows.
WAITING_KEYS: tuple[str, ...] = (
    "blocked", "boardBlocked", "boardHeld", "parked", "epicOpen", "inProgress",
    "tracker", "notNeeded", "assigned", "offQueue", "done", "bot", "untrusted",
    "environment")

# Skip reasons whose bucket needs no further facts off the row.
_DIRECT: dict[str, str] = {
    "parked": "parked",
    "epic_open": "epicOpen",
    "tracker": "tracker",
    "not_needed": "notNeeded",
    "live_session": "inProgress",
    "claimed": "inProgress",
    "bot": "bot",
    "untrusted": "untrusted",
    "environment": "environment",
}
# Skip reasons whose bucket depends on the row's facts.
_ROW_FACTS = frozenset({"blocked", "assigned", "status"})
# Skip reasons that are needs-you, not waiting.
_NEEDS_YOU = frozenset({"held", "needs_spec"})


def owner_logins(policy: Mapping | None) -> frozenset[str]:
    """The product owner's logins: the HUMAN entries of the policy's
    ``trusted_authors`` (never an ``app/…`` or ``…[bot]`` login), lowercased.
    The policy has no separate owner field, and ``trusted_authors`` is the list
    of people allowed to direct agents."""
    return frozenset(a for a in trusted_authors(policy) if not is_automation_author(a))


def assigned_to_owner(assignees: Iterable[str] | None, owners: frozenset[str]) -> bool:
    """Whether any GitHub assignee is one of *owners* (``owner_logins``)."""
    return any(str(a).strip().lower() in owners for a in assignees or ())


def waiting_reason_for_skip(reason: str, row: QueueRow, *,
                            owners: frozenset[str] = frozenset()) -> str | None:
    """The waiting bucket (a ``WAITING_KEYS`` entry) for a row the queue skipped
    as *reason* (a ``queue.SKIP_REASONS`` entry), or ``None`` when the skip is a
    needs-you item rather than waiting: a ``held`` / ``needs_spec`` skip, a board
    Held card that carries a real hold, or an issue assigned to the owner (pass
    the product's ``owner_logins`` as *owners*) whatever it was skipped for —
    ``issue_tier`` makes that needs-you ahead of any blocker, so it is never
    also counted as waiting. Raises ``ValueError`` on an unknown reason so a new
    skip verdict cannot be silently dropped."""
    if reason not in _NEEDS_YOU and reason not in _DIRECT and reason not in _ROW_FACTS:
        raise ValueError(f"unknown queue skip reason {reason!r}")
    if reason in _NEEDS_YOU or assigned_to_owner(row.assignees, owners):
        return None
    if reason in _DIRECT:
        return _DIRECT[reason]
    if reason == "blocked":
        return "blocked" if row.open_blockers else "boardBlocked"
    if reason == "assigned":
        return "assigned"
    # reason == "status": Done / In progress / Held, or below the queue's min_status.
    if row.status == STATUS_IN_PROGRESS:
        return "inProgress"
    if row.status == STATUS_HELD:
        return None if row.hold_kind in HELD_KINDS else "boardHeld"
    if row.status == STATUS_DONE:
        return "done"
    return "offQueue"
