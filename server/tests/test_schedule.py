"""Tests for the pure automation schedule engine (#603)."""
from __future__ import annotations

from datetime import datetime

import pytest

from server.automations.schedule import (
    PRESETS,
    ScheduleError,
    next_runs,
    parse_cron,
    parse_schedule,
    should_tick,
)

# A Wednesday (weekday()==2) at 10:30 local.
WED_1030 = datetime(2026, 7, 15, 10, 30)
# A Saturday (weekday()==5) at 10:30.
SAT_1030 = datetime(2026, 7, 18, 10, 30)


def test_empty_schedule_always_ticks():
    assert should_tick(None, WED_1030) is True
    assert should_tick({}, WED_1030) is True


def test_active_hours_window_gates_ticks():
    sched = {"activeHours": {"start": "09:00", "end": "18:00"}}
    assert should_tick(sched, WED_1030) is True
    assert should_tick(sched, datetime(2026, 7, 15, 8, 59)) is False
    assert should_tick(sched, datetime(2026, 7, 15, 18, 0)) is False  # end exclusive


def test_active_hours_days_restrict_weekdays():
    sched = {"activeHours": {"start": "09:00", "end": "18:00", "days": [0, 1, 2, 3, 4]}}
    assert should_tick(sched, WED_1030) is True   # Wednesday
    assert should_tick(sched, SAT_1030) is False  # Saturday not in days


def test_overnight_window_wraps_midnight():
    sched = {"activeHours": {"start": "22:00", "end": "02:00"}}
    assert should_tick(sched, datetime(2026, 7, 15, 23, 0)) is True
    assert should_tick(sched, datetime(2026, 7, 15, 1, 0)) is True
    assert should_tick(sched, datetime(2026, 7, 15, 12, 0)) is False


def test_end_2400_means_midnight():
    sched = {"activeHours": {"start": "09:00", "end": "24:00"}}
    assert should_tick(sched, datetime(2026, 7, 15, 23, 59)) is True
    assert should_tick(sched, datetime(2026, 7, 15, 8, 0)) is False


def test_malformed_schedule_rejected():
    with pytest.raises(ScheduleError):
        parse_schedule({"activeHours": {"start": "9am", "end": "18:00"}})
    with pytest.raises(ScheduleError):
        parse_schedule({"activeHours": {"start": "09:00", "end": "09:00"}})  # equal
    with pytest.raises(ScheduleError):
        parse_schedule({"activeHours": {"days": [9]}})
    with pytest.raises(ScheduleError):
        parse_schedule({"unknownKey": 1})


def test_cron_parse_and_match_daily_8am():
    cron = parse_cron("0 8 * * *")
    assert cron.matches(datetime(2026, 7, 15, 8, 0)) is True
    assert cron.matches(datetime(2026, 7, 15, 8, 1)) is False
    assert cron.matches(datetime(2026, 7, 15, 9, 0)) is False


def test_cron_step_and_range():
    cron = parse_cron("*/15 9-17 * * 1-5")
    assert cron.matches(datetime(2026, 7, 15, 9, 0)) is True    # Wed 09:00
    assert cron.matches(datetime(2026, 7, 15, 9, 15)) is True
    assert cron.matches(datetime(2026, 7, 15, 9, 10)) is False
    assert cron.matches(datetime(2026, 7, 18, 9, 0)) is False   # Saturday excluded


def test_cron_dow_sunday_zero_or_seven():
    assert parse_cron("0 0 * * 0").matches(datetime(2026, 7, 19, 0, 0))  # Sunday
    assert parse_cron("0 0 * * 7").matches(datetime(2026, 7, 19, 0, 0))  # 7 == Sunday


def test_cron_bad_expr_rejected():
    with pytest.raises(ScheduleError):
        parse_cron("0 8 * *")       # too few fields
    with pytest.raises(ScheduleError):
        parse_cron("99 8 * * *")    # minute out of range
    with pytest.raises(ScheduleError):
        parse_schedule({"cron": "not a cron"})


def test_next_runs_daily_8am():
    runs = next_runs({"cron": "0 8 * * *"}, datetime(2026, 7, 15, 10, 0), count=3)
    assert runs == [
        datetime(2026, 7, 16, 8, 0),
        datetime(2026, 7, 17, 8, 0),
        datetime(2026, 7, 18, 8, 0),
    ]


def test_next_runs_empty_without_cron():
    assert next_runs({"activeHours": {"start": "09:00", "end": "18:00"}}, WED_1030) == []


def test_presets_are_valid_schedules():
    for raw in PRESETS.values():
        parse_schedule(raw)  # must not raise
