"""Pure snooze rules (server/sessions/snooze.py): validation, due-ness, the wake prompt."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.sessions.snooze import (SELF_SNOOZE_MAX, SnoozeError, build_snooze, is_due,
                                    snooze_until, wake_prompt)

NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)


def test_user_snooze_with_a_time_round_trips_as_aware_iso():
    s = build_snooze(until="2026-11-07T14:00:00Z", reason=" PR #9 to land ", by="user", now=NOW)
    assert s == {"at": NOW.isoformat(), "until": "2026-11-07T14:00:00+00:00",
                 "reason": "PR #9 to land", "by": "user"}
    assert snooze_until(s) == datetime(2026, 11, 7, 14, tzinfo=timezone.utc)


def test_user_may_snooze_indefinitely_but_a_session_may_not():
    assert build_snooze(until=None, reason=None, by="user", now=NOW)["until"] is None
    with pytest.raises(SnoozeError, match="until"):
        build_snooze(until=None, reason="waiting on deploy", by="session", now=NOW)


def test_naive_time_is_read_as_utc():
    s = build_snooze(until="2026-11-07T14:00:00", reason=None, by="user", now=NOW)
    assert s["until"] == "2026-11-07T14:00:00+00:00"


@pytest.mark.parametrize("until", ["not a time", "2026-11-06T14:00:00Z", "2028-01-01T00:00:00Z"])
def test_user_snooze_rejects_malformed_past_or_far_future(until):
    with pytest.raises(SnoozeError):
        build_snooze(until=until, reason=None, by="user", now=NOW)


def test_session_snooze_window_is_five_minutes_to_thirty_days():
    with pytest.raises(SnoozeError):
        build_snooze(until=(NOW + timedelta(minutes=2)).isoformat(), reason="x", by="session", now=NOW)
    with pytest.raises(SnoozeError):
        build_snooze(until=(NOW + SELF_SNOOZE_MAX + timedelta(hours=1)).isoformat(),
                     reason="x", by="session", now=NOW)
    ok = build_snooze(until=(NOW + timedelta(days=3)).isoformat(), reason="x", by="session", now=NOW)
    assert ok["by"] == "session"


def test_session_snooze_needs_a_reason():
    with pytest.raises(SnoozeError, match="reason"):
        build_snooze(until=(NOW + timedelta(days=1)).isoformat(), reason="  ", by="session", now=NOW)


def test_unknown_snoozer_is_refused():
    with pytest.raises(SnoozeError):
        build_snooze(until=None, reason=None, by="robot", now=NOW)


def test_overlong_reason_is_refused():
    with pytest.raises(SnoozeError, match="reason"):
        build_snooze(until=None, reason="x" * 501, by="user", now=NOW)


def test_is_due_only_for_a_timed_snooze_whose_time_has_come():
    timed = build_snooze(until="2026-11-06T16:00:00Z", reason=None, by="user", now=NOW)
    assert not is_due(timed, NOW)
    assert is_due(timed, NOW + timedelta(hours=1))
    assert not is_due(build_snooze(until=None, reason=None, by="user", now=NOW),
                      NOW + timedelta(days=999))
    assert not is_due(None, NOW)
    # A naive `now` (the DB clock convention) compares as UTC.
    assert is_due(timed, datetime(2026, 11, 6, 17, 0))


def test_wake_prompt_carries_the_sessions_own_reason_and_next_step():
    s = build_snooze(until="2026-11-07T14:00:00Z", reason="PR #9 to merge", by="session", now=NOW)
    text = wake_prompt(s)
    assert text.startswith("<snooze-wake>") and text.rstrip().endswith("</snooze-wake>")
    assert "<reason>PR #9 to merge</reason>" in text
    assert "snooze" in text and "hand_back" in text
