"""Pure notification routing engine (#602).

DB-free and I/O-free: given an :class:`Event`, a list of :class:`RuleView`
(the ORM :class:`~server.db.models.NotificationRule` rows adapted to a plain
dataclass so this module never imports SQLAlchemy), and the quiet-hours window,
:func:`plan` decides what to do. The API layer and the notify() call site do all
DB access and delivery; this module is the single source of truth for *routing*.

Rule evaluation: only enabled rules are considered, sorted by (priority,
created_at) ascending; the FIRST rule whose event_class AND scope match wins.
event_class ``"*"`` matches any class. A scope field of ``None`` on a rule is a
wildcard; otherwise it must equal the event's corresponding field.

Back-compat: with NO rules configured, everything delivers (default push +
webhook). Configure any rule and unmatched events go silent — that is how a user
gets "only these events, everything else quiet".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# The concrete, emittable event classes. The wildcard "*" is ALSO a valid
# rule.event_class (matches anything) but is intentionally NOT a member here —
# this set is the closed set of classes a call site may actually emit.
EVENT_CLASSES: frozenset[str] = frozenset(
    {
        "needs_input",
        "session_ended",
        "automation_error",
        "automation_warn",
        "pr_broke",
        "pr_merged",
    }
)


@dataclass
class Event:
    """One notifiable thing that happened. Scope fields default to None."""

    event_class: str
    title: str
    body: str
    repo: str | None = None
    account: str | None = None
    automation: str | None = None
    url: str | None = None


@dataclass
class RuleView:
    """A DB-free view of a NotificationRule row (the engine's rule input).

    ``created_at`` need only be comparable (datetime or ISO str) so rules can be
    ordered deterministically after priority.
    """

    id: str
    enabled: bool
    event_class: str
    scope_repo: str | None
    scope_account: str | None
    scope_automation: str | None
    action: str
    channel_id: str | None
    critical: bool
    priority: int
    created_at: object
    once: bool = False
    owner_session_id: str | None = None
    expires_at: datetime | None = None


@dataclass
class Decision:
    """The routing outcome for one event."""

    action: str  # "push" | "channel" | "ignore" | "default"
    channel_id: str | None
    deliver: bool
    suppressed_by_quiet: bool
    # The rule that decided this outcome, or None when no rule matched (the
    # "default"/no-rules-configured and "ignore"/no-match cases). Lets the caller
    # (push.notify_event) disable a fired once-rule without re-querying the DB for
    # which rule won (Task 8.4, spec v2 §4.7 Pack N).
    rule_id: str | None = None
    once: bool = False


def event_class_matches(rule_class: str, event_class: str) -> bool:
    """True if a rule's event_class matches an event's class. "*" matches any."""
    return rule_class == "*" or rule_class == event_class


def scope_matches(rule: RuleView, ev: Event) -> bool:
    """True if every non-None scope field on the rule equals the event's field."""
    if rule.scope_repo is not None and rule.scope_repo != ev.repo:
        return False
    if rule.scope_account is not None and rule.scope_account != ev.account:
        return False
    if rule.scope_automation is not None and rule.scope_automation != ev.automation:
        return False
    return True


def select_rule(rules: list[RuleView], ev: Event, now: datetime | None = None) -> RuleView | None:
    """Return the first enabled, unexpired rule (by priority, then created_at) that matches.

    ``now`` is optional so existing callers are unchanged; pass it to also skip
    a rule whose ``expires_at`` has already passed (an assistant watch's TTL).
    """
    candidates = sorted(
        (r for r in rules if r.enabled),
        key=lambda r: (r.priority, _sort_key(r.created_at)),
    )
    for rule in candidates:
        if now is not None and rule.expires_at is not None and rule.expires_at < now:
            continue
        if event_class_matches(rule.event_class, ev.event_class) and scope_matches(rule, ev):
            return rule
    return None


def _sort_key(created_at: object):
    """Return a value usable as a secondary sort key across datetime/str/None."""
    if created_at is None:
        return ""
    return created_at


def _parse_hhmm(value: str) -> int:
    """Parse "HH:MM" to minutes-since-midnight."""
    hh, mm = value.split(":", 1)
    return int(hh) * 60 + int(mm)


def in_quiet_hours(start: str | None, end: str | None, now: datetime) -> bool:
    """True if ``now`` falls inside the quiet-hours window.

    Either bound None → disabled (False). The window is inclusive of start and
    exclusive of end. A window whose end <= start wraps past midnight (e.g.
    23:00–07:00 covers 23:00-23:59 and 00:00-06:59).
    """
    if start is None or end is None:
        return False
    start_m = _parse_hhmm(start)
    end_m = _parse_hhmm(end)
    now_m = now.hour * 60 + now.minute
    if end_m > start_m:
        return start_m <= now_m < end_m
    # Wrap (end <= start): inside if at/after start OR before end.
    return now_m >= start_m or now_m < end_m


def plan(
    ev: Event,
    rules: list[RuleView],
    quiet_start: str | None,
    quiet_end: str | None,
    now: datetime,
) -> Decision:
    """Decide delivery for ``ev`` given the rule set and quiet-hours window."""
    rule = select_rule(rules, ev, now)
    if rule is None:
        if not rules:
            # Back-compat: no rules configured ⇒ default push + webhook.
            return Decision(
                action="default", channel_id=None, deliver=True, suppressed_by_quiet=False
            )
        # Rules exist but none matched ⇒ everything-else-silent.
        return Decision(action="ignore", channel_id=None, deliver=False, suppressed_by_quiet=False)

    if rule.action == "ignore":
        return Decision(action="ignore", channel_id=None, deliver=False, suppressed_by_quiet=False,
                        rule_id=rule.id, once=rule.once)

    # push | channel
    channel_id = rule.channel_id if rule.action == "channel" else None
    quiet = in_quiet_hours(quiet_start, quiet_end, now) and not rule.critical
    if quiet:
        return Decision(
            action=rule.action, channel_id=channel_id, deliver=False, suppressed_by_quiet=True,
            rule_id=rule.id, once=rule.once,
        )
    return Decision(
        action=rule.action, channel_id=channel_id, deliver=True, suppressed_by_quiet=False,
        rule_id=rule.id, once=rule.once,
    )
