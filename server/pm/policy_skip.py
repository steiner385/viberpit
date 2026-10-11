"""Holds the Backlog Runner wrote on issues a product's policy now skips
(cairnea/KinDash#16652).

The queue skips an issue as ``bot`` when its labels meet the built-in bot-noise
filter or the product's own ``skip_labels`` (``queue.is_bot_noise``). But a hold is
written once and read until released, and ``skip_labels`` can change after the
fact: KinDash listed ``forced-deploy-audit`` on 2026-10-03, two weeks after the
runner had handed seventeen such issues to their owner as "bounced at opus; fable
needs approval". Those hand-offs stayed in the Held inbox with an "Approve
escalation" tap, and the tap (``escalation_approval.approve_escalation`` →
``backlog_runner_dispatch``) dispatches WITHOUT re-ranking, so on 2026-10-06 five
of them were dispatched again — each a session that could only report "no code to
write".

Pure half. ``skipped_by_policy`` is the approve tap's predicate (the not-work rule
plus author trust). The backlog tick ends every RUNNER-written ``owner_action`` hold
the not-work rule alone names (``stale_runner_holds``: ``not_work_by_policy`` — the
``bot`` and ``tracker`` skips under the product's policy; trust is NOT a release
reason), so the inbox stops offering them and the queue's ordinary skip takes over.
Only the runner's own holds: a person who parked a skipped issue meant to.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from server.automations.product_policy import (skip_labels, skips_self_closing_trackers,
                                               trusted_authors)
from server.pm.holds import RUNNER_ACTOR
from server.pm.queue import (QueueRow, is_bot_noise, is_parked, is_self_closing_tracker,
                             is_trusted_author)


def not_work_by_policy(*, author: str, labels: Iterable[str], title: str,
                       policy: Mapping | None) -> bool:
    """Whether the queue skips this issue as NOT WORK under *policy*: ``bot``
    (the built-in filter plus the product's ``skip_labels``), ``tracker`` (a
    self-closing tracker, unless the product opted back in) or parked by a
    person's ``parked`` label — ``queue.skip_reason``'s verdicts for the issue
    itself, through the same predicates. Author trust
    is not part of it (see ``stale_runner_holds``). The runner's not-needed settle
    reads it too: an issue the queue will never dispatch needs no owner hold to
    keep it from re-dispatching (#2683)."""
    labels = tuple(labels)
    if is_parked(labels) or is_bot_noise(author=author, labels=labels, title=title,
                                         extra_skip_labels=skip_labels(policy)):
        return True
    return skips_self_closing_trackers(policy) and is_self_closing_tracker(labels, title)


def skipped_by_policy(*, author: str, labels: Iterable[str], title: str,
                      policy: Mapping | None) -> bool:
    """Whether the queue would skip this issue as ``bot``, ``tracker`` or
    ``untrusted`` under *policy* — ``not_work_by_policy``, plus an author outside its
    ``trusted_authors``. ``None`` (no policy for the product) applies the built-in
    filters and trusts only automation logins (fail-closed)."""
    if not is_trusted_author(author, trusted_authors(policy)):
        return True
    return not_work_by_policy(author=author, labels=labels, title=title, policy=policy)


#: The reason the runner wrote, before #2683, when a session found an OPEN issue
#: needed no PR ("session found no PR is needed (not-needed): <note>"). It no
#: longer writes such a hold — not-needed suppression keeps the issue out of the
#: queue instead — so this is read only to clear the ones already written.
NOT_NEEDED_HOLD_PREFIX = "session found no PR is needed"


def _not_needed_hold_still_stands(row: QueueRow) -> bool:
    return (row.not_needed_standing
            and (row.hold_reason or "").startswith(NOT_NEEDED_HOLD_PREFIX))


def stale_runner_holds(rows: Iterable[QueueRow],
                       policies: Mapping[str, Mapping]) -> list[QueueRow]:
    """The rows whose ``owner_action`` hold the RUNNER wrote and whose issue the
    owning product's policy now skips as ``bot`` or ``tracker``
    (``not_work_by_policy``), or whose "session found no PR is needed" hold is
    covered by a not-needed verdict that still stands (#2683: the queue suppresses
    the issue on its own until it materially changes), in the order given. A person's hold,
    a hold of another kind, and an issue the policy still treats as work are not stale.
    Author TRUST is deliberately not part of this: an untrusted author's issue is not
    dispatched, but the owner's inbox must not be emptied because a product has not
    (yet) listed its trusted authors."""
    return [row for row in rows
            if row.hold_kind == "owner_action" and row.hold_by == RUNNER_ACTOR
            and (_not_needed_hold_still_stands(row)
                 or not_work_by_policy(author=row.author, labels=row.labels,
                                       title=row.title, policy=policies.get(row.product)))]
