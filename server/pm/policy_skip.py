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
from datetime import datetime

from server.automations.product_policy import (skip_labels, skips_self_closing_trackers,
                                               trusted_authors)
from server.pm.holds import RUNNER_ACTOR
from server.pm.queue import (QueueRow, is_bot_noise, is_parked, is_self_closing_tracker,
                             is_trusted_author, needs_unavailable_environment)


def not_work_by_policy(*, author: str, labels: Iterable[str], title: str,
                       policy: Mapping | None) -> bool:
    """Whether the queue skips this issue as NOT WORK under *policy*: ``bot``
    (the built-in filter plus the product's ``skip_labels``) or ``tracker`` (a
    self-closing tracker, unless the product opted back in) — ``queue.skip_reason``'s
    verdicts on what the issue IS, through the same predicates. A person's
    ``parked`` label is not part of it: that is a park, not a verdict that the issue
    is not work, and it must not end the runner's own hand-off hold (#2683 review;
    ``skipped_by_policy`` and the not-needed settle add it themselves). Author trust
    is not part of it either (see ``stale_runner_holds``)."""
    labels = tuple(labels)
    if is_bot_noise(author=author, labels=labels, title=title,
                    extra_skip_labels=skip_labels(policy)):
        return True
    return (skips_self_closing_trackers(policy)
            and is_self_closing_tracker(labels, title, author=author))


def skipped_by_policy(*, author: str, labels: Iterable[str], title: str,
                      policy: Mapping | None) -> bool:
    """Whether the queue would skip this issue as ``bot``, ``tracker``, ``parked``,
    ``untrusted`` or ``environment`` under *policy* — ``not_work_by_policy``, a
    person's ``parked`` label, an author outside its ``trusted_authors``, or an
    environment gate no fleet host meets (#2983): the approve-escalation tap's
    refusal. ``None`` (no policy for the product) applies the built-in filters and
    trusts only automation logins (fail-closed)."""
    if not is_trusted_author(author, trusted_authors(policy)):
        return True
    labels = tuple(labels)
    return (is_parked(labels) or needs_unavailable_environment(labels)
            or not_work_by_policy(author=author, labels=labels, title=title, policy=policy))


#: The reason the runner wrote, before #2683, when a session found an OPEN issue
#: needed no PR ("session found no PR is needed (not-needed): <note>"). It no
#: longer writes such a hold — not-needed suppression keeps the issue out of the
#: queue instead — so this is read only to clear the ones already written.
NOT_NEEDED_HOLD_PREFIX = "session found no PR is needed"


#: How many "no PR is needed" holds whose suppression window has ALREADY ended one
#: hygiene pass may release (#2683 review). Releasing such a hold makes the issue
#: dispatchable at once, so a pass that released them all after a deploy would
#: burst-dispatch every one; one per pass (the backlog tick) drips them out.
EXPIRED_NOT_NEEDED_RELEASES_PER_PASS = 1


def _is_not_needed_hold(row: QueueRow) -> bool:
    return (row.not_needed_standing
            and (row.hold_reason or "").startswith(NOT_NEEDED_HOLD_PREFIX))


def stale_runner_holds(rows: Iterable[QueueRow], policies: Mapping[str, Mapping], *,
                       now: datetime) -> list[QueueRow]:
    """The RUNNER-written ``owner_action`` holds the hygiene pass ends, in the order
    given:

    - the owning product's policy now skips the issue as ``bot`` or ``tracker``
      (``not_work_by_policy``) — the queue's ordinary skip takes over;
    - a "session found no PR is needed" hold whose not-needed verdict still stands
      (#2683): while its suppression window is open the issue stays skipped as
      ``not_needed`` after the release, so releasing it changes nothing but the
      inbox. Once the window has ended, release makes the issue dispatchable at
      once, so at most ``EXPIRED_NOT_NEEDED_RELEASES_PER_PASS`` of those go per pass.

    A person's hold, a hold of another kind, and an issue the policy still treats as
    work are not stale. Neither is a runner hold on an issue a person has since
    labelled ``parked`` — the label keeps it out of the queue, and the hand-off is
    still the owner's to read (#2683 review). Author TRUST is deliberately not part
    of this: an untrusted author's issue is not dispatched, but the owner's inbox
    must not be emptied because a product has not (yet) listed its trusted
    authors."""
    out: list[QueueRow] = []
    expired_budget = EXPIRED_NOT_NEEDED_RELEASES_PER_PASS
    for row in rows:
        if row.hold_kind != "owner_action" or row.hold_by != RUNNER_ACTOR:
            continue
        if not_work_by_policy(author=row.author, labels=row.labels, title=row.title,
                              policy=policies.get(row.product)):
            out.append(row)
        elif _is_not_needed_hold(row):
            if row.not_needed_until is not None and now < row.not_needed_until:
                out.append(row)
            elif expired_budget > 0:
                expired_budget -= 1
                out.append(row)
    return out
