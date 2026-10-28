"""Unit tests for the pure notification routing engine (#602)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from server.sessions.notify_rules import (
    EVENT_CLASSES,
    Event,
    RuleView,
    event_class_matches,
    in_quiet_hours,
    localize_now,
    is_expired,
    is_watch_rule,
    plan,
    plan_watches,
    rule_view_from_row,
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
    watch=False,
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
        watch=watch,
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
# localize_now (#2562)                                                        #
# --------------------------------------------------------------------------- #
def test_localize_now_none_tz_returns_now_unchanged():
    now = datetime(2024, 1, 1, 5, 0, tzinfo=timezone.utc)
    assert localize_now(now, None) is now


def test_localize_now_converts_utc_to_named_zone():
    # 05:00 UTC in winter is 00:00 America/New_York (UTC-5).
    now = datetime(2024, 1, 1, 5, 0, tzinfo=timezone.utc)
    local = localize_now(now, "America/New_York")
    assert (local.hour, local.minute) == (0, 0)


def test_localize_now_unknown_tz_falls_back_to_now_unchanged():
    now = datetime(2024, 1, 1, 5, 0, tzinfo=timezone.utc)
    assert localize_now(now, "Not/AZone") is now


def test_quiet_hours_bug_scenario_from_2562():
    """22:00-07:00 entered by an America/New_York operator was evaluated in bare
    UTC and fired a non-critical push at 05:00 local, still inside the intended
    window (#2562)."""
    # 05:00 America/New_York (EDT, UTC-4 in July) == 09:00 UTC.
    now_utc = datetime(2024, 7, 1, 9, 0, tzinfo=timezone.utc)
    assert not in_quiet_hours("22:00", "07:00", now_utc)  # the bug: bare UTC reads as not-quiet → pings
    local = localize_now(now_utc, "America/New_York")
    assert in_quiet_hours("22:00", "07:00", local)  # fixed: local reads as quiet → suppressed


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


# --------------------------------------------------------------------------- #
# Assistant watches are additive to routing (#3181)                          #
# --------------------------------------------------------------------------- #
def _watch(id="w", **kw) -> RuleView:
    return _rule(id, watch=True, action="channel", channel_id="assistant-chan", **kw)


def test_plan_with_only_watches_keeps_the_default_fanout():
    """A watch is not a routing rule: arming one must not flip every other event
    from the default fanout to silence."""
    d = plan(_ev("needs_input"), [_watch(event_class="pr_broke")], None, None, NOW)
    assert d.action == "default" and d.deliver is True


def test_plan_spent_watches_do_not_mute_the_default_fanout():
    spent = _watch("spent", event_class="session_ended", enabled=False, once=True)
    expired = _watch("expired", event_class="session_ended",
                     expires_at=NOW - timedelta(hours=1))
    d = plan(_ev("needs_input"), [spent, expired], None, None, NOW)
    assert d.action == "default" and d.deliver is True


def test_plan_watch_never_preempts_a_users_own_rule():
    users = _rule("mine", event_class="needs_input", action="push", priority=100)
    w = _watch("w", event_class="needs_input", priority=1)
    d = plan(_ev("needs_input"), [w, users], None, None, NOW)
    assert d.action == "push" and d.rule_id == "mine"


def test_plan_watches_returns_every_armed_match_only():
    a = _watch("a", event_class="session_ended", scope_session_id="s1", once=True)
    b = _watch("b", event_class="session_ended", scope_session_id="s1")
    other_session = _watch("c", event_class="session_ended", scope_session_id="s2")
    spent = _watch("d", event_class="session_ended", enabled=False)
    expired = _watch("e", event_class="session_ended", expires_at=NOW - timedelta(hours=1))
    routing = _rule("r", event_class="session_ended", action="push")
    got = plan_watches(_ev("session_ended", session_id="s1"),
                       [a, b, other_session, spent, expired, routing], None, None, NOW)
    assert [d.rule_id for d in got] == ["a", "b"]
    assert all(d.action == "channel" and d.deliver and d.channel_id == "assistant-chan"
               for d in got)
    assert got[0].once is True and got[1].once is False


def test_plan_watches_quiet_hours_suppress_non_critical_watches():
    quiet = datetime(2024, 1, 1, 23, 30)
    soft = _watch("soft", event_class="needs_input")
    loud = _watch("loud", event_class="needs_input", critical=True)
    got = {d.rule_id: d for d in plan_watches(_ev(), [soft, loud], "23:00", "07:00", quiet)}
    assert got["soft"].deliver is False and got["soft"].suppressed_by_quiet is True
    assert got["loud"].deliver is True


def test_rule_view_from_row_carries_every_scope():
    """The one adapter: the routing preview's old private copy dropped
    scope_session_id, so a session watch previewed as matching every session."""
    from types import SimpleNamespace
    row = SimpleNamespace(
        id="x", enabled=True, event_class="session_ended", scope_repo="o/r",
        scope_account="acct", scope_automation="auto", action="channel",
        channel_id="c", critical=False, priority=5, created_at=NOW, once=True,
        scope_session_id="s1", owner_session_id="a1", expires_at=NOW)
    v = rule_view_from_row(row, watch=True)
    assert (v.scope_session_id, v.owner_session_id, v.expires_at, v.once, v.watch) == \
        ("s1", "a1", NOW, True, True)
    assert not scope_matches(v, _ev("session_ended", session_id="s2"))


def test_is_watch_rule_keys_off_the_channel_kind():
    from types import SimpleNamespace
    channels = {"a": SimpleNamespace(kind="assistant"), "h": SimpleNamespace(kind="webhook")}
    assert is_watch_rule(SimpleNamespace(channel_id="a"), channels)
    assert not is_watch_rule(SimpleNamespace(channel_id="h"), channels)
    assert not is_watch_rule(SimpleNamespace(channel_id=None), channels)
    assert not is_watch_rule(SimpleNamespace(channel_id="gone"), channels)


def test_is_expired_tolerates_naive_and_aware_mixes():
    """SQLite returns DateTime(timezone=True) naive; a raw `<` against an aware now
    raised TypeError and failed the whole routing pass open."""
    aware_now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert is_expired(datetime(2024, 1, 1, 11, 0), aware_now)
    assert not is_expired(datetime(2024, 1, 1, 13, 0), aware_now)
    assert is_expired(datetime(2024, 1, 1, 11, 0, tzinfo=timezone.utc), datetime(2024, 1, 1, 12, 0))
    assert not is_expired(None, aware_now)
