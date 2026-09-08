"""Unit tests for the pure notification routing engine (#602)."""
from __future__ import annotations

from datetime import datetime, timedelta

from server.sessions.notify_rules import (
    EVENT_CLASSES,
    Event,
    RuleView,
    event_class_matches,
    in_quiet_hours,
    plan,
    scope_matches,
    select_rule,
)


def _rule(
    id="r",
    *,
    enabled=True,
    event_class="*",
    scope_repo=None,
    scope_account=None,
    scope_automation=None,
    action="push",
    channel_id=None,
    critical=False,
    priority=100,
    created_at="2024-01-01T00:00:00",
    once=False,
    owner_session_id=None,
    expires_at=None,
    scope_session_id=None,
) -> RuleView:
    return RuleView(
        id=id,
        enabled=enabled,
        event_class=event_class,
        scope_repo=scope_repo,
        scope_account=scope_account,
        scope_automation=scope_automation,
        action=action,
        channel_id=channel_id,
        critical=critical,
        priority=priority,
        created_at=created_at,
        once=once,
        owner_session_id=owner_session_id,
        expires_at=expires_at,
        scope_session_id=scope_session_id,
    )


def _ev(event_class="needs_input", **kw) -> Event:
    return Event(event_class=event_class, title="t", body="b", **kw)


# --------------------------------------------------------------------------- #
# event_class / scope matching                                                #
# --------------------------------------------------------------------------- #
def test_event_class_matches_wildcard_and_exact():
    assert event_class_matches("*", "needs_input")
    assert event_class_matches("needs_input", "needs_input")
    assert not event_class_matches("needs_input", "session_ended")


def test_wildcard_star_not_in_concrete_event_classes():
    assert "*" not in EVENT_CLASSES
    assert "needs_input" in EVENT_CLASSES


def test_scope_none_is_wildcard_and_exact_must_equal():
    ev = _ev(repo="o/r", account="acct", automation="autopilot")
    assert scope_matches(_rule(), ev)  # all None → wildcard
    assert scope_matches(_rule(scope_repo="o/r"), ev)
    assert not scope_matches(_rule(scope_repo="o/other"), ev)
    assert scope_matches(_rule(scope_account="acct"), ev)
    assert not scope_matches(_rule(scope_account="nope"), ev)
    assert scope_matches(_rule(scope_automation="autopilot"), ev)
    assert not scope_matches(_rule(scope_automation="pr-doctor"), ev)


def test_scope_session_id_matches_like_the_other_scope_fields():
    ev = _ev(event_class="session_ended", session_id="sess-1")
    assert scope_matches(_rule(), ev)  # None → wildcard
    assert scope_matches(_rule(scope_session_id="sess-1"), ev)
    assert not scope_matches(_rule(scope_session_id="sess-2"), ev)


def test_scope_exact_requires_event_field_present():
    # Rule pins a repo but the event has none → no match.
    assert not scope_matches(_rule(scope_repo="o/r"), _ev(repo=None))


# --------------------------------------------------------------------------- #
# select_rule ordering + first-match-wins                                     #
# --------------------------------------------------------------------------- #
def test_select_rule_priority_ordering_first_match_wins():
    lo = _rule(id="lo", priority=10, action="channel", channel_id="c1")
    hi = _rule(id="hi", priority=200, action="push")
    chosen = select_rule([hi, lo], _ev())
    assert chosen is not None and chosen.id == "lo"


def test_select_rule_created_at_breaks_priority_tie():
    a = _rule(id="a", priority=100, created_at="2024-01-01T00:00:00")
    b = _rule(id="b", priority=100, created_at="2023-06-01T00:00:00")
    chosen = select_rule([a, b], _ev())
    assert chosen is not None and chosen.id == "b"  # earlier created_at first


def test_select_rule_skips_disabled():
    disabled = _rule(id="off", enabled=False, priority=1)
    on = _rule(id="on", priority=100)
    chosen = select_rule([disabled, on], _ev())
    assert chosen is not None and chosen.id == "on"


def test_select_rule_returns_none_when_no_match():
    assert select_rule([_rule(event_class="pr_merged")], _ev("needs_input")) is None


def test_rule_view_watch_fields_default_to_unset():
    r = _rule()
    assert r.once is False
    assert r.owner_session_id is None
    assert r.expires_at is None


def test_select_rule_skips_expired_when_now_given():
    now = datetime(2024, 1, 1, 12, 0)
    expired = _rule(id="expired", priority=1, expires_at=now - timedelta(hours=1))
    live = _rule(id="live", priority=100)
    chosen = select_rule([expired, live], _ev(), now=now)
    assert chosen is not None and chosen.id == "live"


def test_select_rule_keeps_expired_rule_when_now_omitted():
    # Back-compat: existing callers that don't pass `now` never filter by expiry.
    expired = _rule(id="expired", priority=1, expires_at=datetime(2000, 1, 1))
    chosen = select_rule([expired], _ev())
    assert chosen is not None and chosen.id == "expired"


def test_select_rule_keeps_rule_expiring_exactly_now():
    now = datetime(2024, 1, 1, 12, 0)
    at_now = _rule(id="at-now", expires_at=now)
    chosen = select_rule([at_now], _ev(), now=now)
    assert chosen is not None and chosen.id == "at-now"


# --------------------------------------------------------------------------- #
# plan: empty vs unmatched vs matched                                         #
# --------------------------------------------------------------------------- #
NOW = datetime(2024, 1, 1, 12, 0)


def test_plan_empty_rules_defaults_to_deliver():
    d = plan(_ev(), [], None, None, NOW)
    assert d.action == "default" and d.deliver is True and d.suppressed_by_quiet is False


def test_plan_nonempty_no_match_is_ignore_silent():
    d = plan(_ev("session_ended"), [_rule(event_class="needs_input")], None, None, NOW)
    assert d.action == "ignore" and d.deliver is False


def test_plan_ignore_rule_silences():
    d = plan(_ev(), [_rule(action="ignore")], None, None, NOW)
    assert d.action == "ignore" and d.deliver is False


def test_plan_push_delivers():
    d = plan(_ev(), [_rule(action="push")], None, None, NOW)
    assert d.action == "push" and d.deliver is True and d.channel_id is None


def test_plan_channel_carries_channel_id():
    d = plan(_ev(), [_rule(action="channel", channel_id="c9")], None, None, NOW)
    assert d.action == "channel" and d.deliver is True and d.channel_id == "c9"


def test_plan_skips_expired_watch_rule():
    expired = _rule(action="channel", channel_id="watch", expires_at=NOW - timedelta(hours=1))
    d = plan(_ev(), [expired], None, None, NOW)
    assert d.action == "ignore" and d.deliver is False


# --------------------------------------------------------------------------- #
# quiet hours                                                                 #
# --------------------------------------------------------------------------- #
def test_in_quiet_hours_disabled_when_either_none():
    assert not in_quiet_hours(None, "07:00", NOW)
    assert not in_quiet_hours("23:00", None, NOW)


def test_in_quiet_hours_same_day_window_inclusive_start_exclusive_end():
    assert in_quiet_hours("09:00", "17:00", datetime(2024, 1, 1, 9, 0))  # start inclusive
    assert in_quiet_hours("09:00", "17:00", datetime(2024, 1, 1, 16, 59))
    assert not in_quiet_hours("09:00", "17:00", datetime(2024, 1, 1, 17, 0))  # end exclusive
    assert not in_quiet_hours("09:00", "17:00", datetime(2024, 1, 1, 8, 59))


def test_in_quiet_hours_overnight_wrap():
    assert in_quiet_hours("23:00", "07:00", datetime(2024, 1, 1, 2, 0))  # 02:00 → quiet
    assert in_quiet_hours("23:00", "07:00", datetime(2024, 1, 1, 23, 30))
    assert not in_quiet_hours("23:00", "07:00", datetime(2024, 1, 1, 12, 0))  # noon → not quiet
    assert not in_quiet_hours("23:00", "07:00", datetime(2024, 1, 1, 7, 0))  # end exclusive


def test_plan_quiet_hours_suppress_push_but_critical_bypasses():
    quiet_now = datetime(2024, 1, 1, 2, 0)
    suppressed = plan(_ev(), [_rule(action="push")], "23:00", "07:00", quiet_now)
    assert suppressed.deliver is False and suppressed.suppressed_by_quiet is True

    crit = plan(_ev(), [_rule(action="push", critical=True)], "23:00", "07:00", quiet_now)
    assert crit.deliver is True and crit.suppressed_by_quiet is False


def test_plan_overnight_noon_not_suppressed():
    noon = datetime(2024, 1, 1, 12, 0)
    d = plan(_ev(), [_rule(action="push")], "23:00", "07:00", noon)
    assert d.deliver is True and d.suppressed_by_quiet is False


# --------------------------------------------------------------------------- #
# Acceptance scenario                                                         #
# --------------------------------------------------------------------------- #
def test_acceptance_scenario_routes_by_event_class():
    rules = [
        _rule(id="err", event_class="automation_error", action="channel", channel_id="ops", priority=10),
        _rule(id="input", event_class="needs_input", action="push", priority=20),
        _rule(id="catch", event_class="*", action="ignore", priority=1000),
    ]
    err = plan(_ev("automation_error"), rules, None, None, NOW)
    assert err.action == "channel" and err.channel_id == "ops" and err.deliver is True

    inp = plan(_ev("needs_input"), rules, None, None, NOW)
    assert inp.action == "push" and inp.deliver is True

    ended = plan(_ev("session_ended"), rules, None, None, NOW)
    assert ended.action == "ignore" and ended.deliver is False


def test_acceptance_scenario_without_catchall_is_still_silent():
    rules = [
        _rule(id="err", event_class="automation_error", action="channel", channel_id="ops"),
        _rule(id="input", event_class="needs_input", action="push"),
    ]
    ended = plan(_ev("session_ended"), rules, None, None, NOW)
    assert ended.action == "ignore" and ended.deliver is False
