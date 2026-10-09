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

Pure half. ``skipped_by_policy`` is the one predicate both consumers apply: the
approve tap refuses on it, and the backlog tick ends every RUNNER-written
``owner_action`` hold it names (``stale_runner_holds``), so the inbox stops
offering them and the queue's ordinary ``bot`` skip takes over. Only the runner's
own holds: a person who parked a skipped issue meant to.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from server.automations.product_policy import skip_labels, trusted_authors
from server.pm.holds import RUNNER_ACTOR
from server.pm.queue import QueueRow, is_bot_noise, is_trusted_author


def skipped_by_policy(*, author: str, labels: Iterable[str], title: str,
                      policy: Mapping | None) -> bool:
    """Whether the queue would skip this issue as ``bot`` or ``untrusted`` under
    *policy* — the built-in filter plus the product's ``skip_labels``, and an author
    outside its ``trusted_authors``. ``None`` (no policy for the product) applies the
    built-in filter and trusts only automation logins (fail-closed)."""
    if not is_trusted_author(author, trusted_authors(policy)):
        return True
    return is_bot_noise(author=author, labels=labels, title=title,
                        extra_skip_labels=skip_labels(policy))


def stale_runner_holds(rows: Iterable[QueueRow],
                       policies: Mapping[str, Mapping]) -> list[QueueRow]:
    """The rows whose ``owner_action`` hold the RUNNER wrote and whose issue the
    owning product's policy now skips, in the order given. A person's hold, a hold
    of another kind, and an issue the policy still treats as work are not stale."""
    return [row for row in rows
            if row.hold_kind == "owner_action" and row.hold_by == RUNNER_ACTOR
            and skipped_by_policy(author=row.author, labels=row.labels, title=row.title,
                                  policy=policies.get(row.product))]
