"""Pure schedule engine for user-configurable automation cadence (#603).

Two orthogonal concepts, both optional and both stored on ``Automation.schedule``:

* **Active-hours window** — gates a *continuous* automation (pr-doctor, backlog
  runner…). The runner calls :func:`should_tick` before each tick; outside the
  window the tick is skipped (and the skip is surfaced in the activity feed).
* **Cron expression** — a discrete fire schedule used for the next-runs preview
  in the UI and (future) scheduled dispatch. :func:`next_runs` computes upcoming
  fire times so the editor can show "next 3 runs".

Everything here is pure: callers pass ``now`` in, so the module is deterministic
and unit-testable with no clock. Times are treated in whatever tz ``now`` carries
(the caller passes local time); no tz math happens here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# activeHours.days uses Python's weekday(): 0 = Monday … 6 = Sunday.
_WEEKDAY_MIN, _WEEKDAY_MAX = 0, 6


class ScheduleError(ValueError):
    """Raised on a malformed schedule config (bad time, bad cron, bad days)."""


@dataclass(frozen=True)
class ActiveHours:
    start_min: int          # minutes past midnight, 0..1439
    end_min: int            # minutes past midnight, 1..1440 (exclusive)
    days: frozenset[int]    # empty = every day; else subset of 0..6 (Mon..Sun)


def _parse_hhmm(value: object, *, field: str) -> int:
    if not isinstance(value, str) or ":" not in value:
        raise ScheduleError(f"{field}: expected 'HH:MM', got {value!r}")
    hh, _, mm = value.partition(":")
    try:
        h, m = int(hh), int(mm)
    except ValueError:
        raise ScheduleError(f"{field}: expected 'HH:MM', got {value!r}")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ScheduleError(f"{field}: {value!r} out of range")
    return h * 60 + m


def _parse_active_hours(raw: object) -> ActiveHours | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ScheduleError("activeHours must be an object")
    start = _parse_hhmm(raw.get("start", "00:00"), field="activeHours.start")
    end_raw = raw.get("end", "24:00")
    end = 1440 if end_raw == "24:00" else _parse_hhmm(end_raw, field="activeHours.end")
    days_raw = raw.get("days") or []
    if not isinstance(days_raw, list) or not all(isinstance(d, int) for d in days_raw):
        raise ScheduleError("activeHours.days must be a list of ints (0=Mon..6=Sun)")
    days = frozenset(int(d) for d in days_raw)
    if any(d < _WEEKDAY_MIN or d > _WEEKDAY_MAX for d in days):
        raise ScheduleError("activeHours.days entries must be 0..6")
    if start == end:
        raise ScheduleError("activeHours start and end must differ")
    return ActiveHours(start_min=start, end_min=end, days=days)


@dataclass(frozen=True)
class Schedule:
    active_hours: ActiveHours | None
    cron: str | None

    @property
    def is_empty(self) -> bool:
        return self.active_hours is None and self.cron is None


def parse_schedule(raw: dict | None) -> Schedule:
    """Validate + normalize a stored schedule dict. Raises ScheduleError on bad input.

    ``None`` / ``{}`` → an empty schedule (no gating, no cron)."""
    if not raw:
        return Schedule(active_hours=None, cron=None)
    if not isinstance(raw, dict):
        raise ScheduleError("schedule must be an object")
    unknown = set(raw) - {"activeHours", "cron"}
    if unknown:
        raise ScheduleError(f"unknown schedule keys: {sorted(unknown)}")
    active = _parse_active_hours(raw.get("activeHours"))
    cron_raw = raw.get("cron")
    cron: str | None = None
    if cron_raw not in (None, ""):
        if not isinstance(cron_raw, str):
            raise ScheduleError("cron must be a string")
        parse_cron(cron_raw)  # validate now so a bad cron is rejected on save
        cron = cron_raw.strip()
    return Schedule(active_hours=active, cron=cron)


# ---------------------------------------------------------------------------
# Active-hours gating
# ---------------------------------------------------------------------------


def should_tick(raw: dict | None, now: datetime) -> bool:
    """Whether a continuous automation may tick at ``now`` given its schedule.

    Empty schedule / no active-hours window → always True (unrestricted). A
    window with ``end`` <= ``start`` is treated as overnight (wraps midnight)."""
    sched = raw if isinstance(raw, Schedule) else parse_schedule(raw)  # type: ignore[arg-type]
    ah = sched.active_hours
    if ah is None:
        return True
    minute = now.hour * 60 + now.minute
    if ah.days and now.weekday() not in ah.days:
        # For an overnight window the "day" is the day the window OPENED; a match
        # on the current weekday is a good-enough approximation for gating and
        # keeps the rule intuitive ("weekdays 22:00-02:00" runs Mon-Fri nights).
        return False
    if ah.start_min < ah.end_min:
        return ah.start_min <= minute < ah.end_min
    # Overnight wrap: active if after start OR before end.
    return minute >= ah.start_min or minute < ah.end_min


# ---------------------------------------------------------------------------
# Cron (5-field: minute hour day-of-month month day-of-week)
# ---------------------------------------------------------------------------

_CRON_BOUNDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]  # dow 0/7 = Sunday


def _parse_cron_field(field: str, lo: int, hi: int, *, is_dow: bool) -> set[int]:
    out: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) == 0:
                raise ScheduleError(f"cron: bad step {step_s!r}")
            step = int(step_s)
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise ScheduleError(f"cron: bad range {part!r}")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise ScheduleError(f"cron: bad field {part!r}")
        for v in range(start, end + 1, step):
            if is_dow and v == 7:
                v = 0
            if v < lo or v > hi:
                raise ScheduleError(f"cron: {v} out of range {lo}-{hi}")
            out.add(v)
    return out


@dataclass(frozen=True)
class Cron:
    minute: frozenset[int]
    hour: frozenset[int]
    dom: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]
    # Standard cron rule: when BOTH dom and dow are restricted, a datetime matches
    # if EITHER matches; if only one is restricted, only it applies.
    dom_restricted: bool
    dow_restricted: bool

    def matches(self, dt: datetime) -> bool:
        if dt.minute not in self.minute or dt.hour not in self.hour:
            return False
        if dt.month not in self.month:
            return False
        # cron dow: 0 = Sunday; Python weekday(): Monday = 0 → convert.
        cron_dow = (dt.weekday() + 1) % 7
        dom_ok = dt.day in self.dom
        dow_ok = cron_dow in self.dow
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok


def parse_cron(expr: str) -> Cron:
    fields = expr.split()
    if len(fields) != 5:
        raise ScheduleError("cron must have 5 fields: 'min hour dom mon dow'")
    sets = [
        _parse_cron_field(f, lo, hi, is_dow=(i == 4))
        for i, (f, (lo, hi)) in enumerate(zip(fields, _CRON_BOUNDS))
    ]
    return Cron(
        minute=frozenset(sets[0]), hour=frozenset(sets[1]), dom=frozenset(sets[2]),
        month=frozenset(sets[3]), dow=frozenset(sets[4]),
        dom_restricted=fields[2] != "*", dow_restricted=fields[4] != "*",
    )


# ---------------------------------------------------------------------------
# Recurring-session dispatch (#603): decide when a cron fires a real session
# ---------------------------------------------------------------------------

# How late a cron firing may be caught up on recovery before it is dropped.
# With a sub-minute dispatch tick this only matters across a daemon restart: a
# run missed by less than the grace fires once when the daemon comes back; an
# older one is skipped rather than replayed as a burst.
DEFAULT_CATCHUP_GRACE = timedelta(minutes=30)


@dataclass(frozen=True)
class FireDecision:
    """Outcome of :func:`evaluate_fire`. ``watermark`` is the value to persist to
    ``Automation.last_fired_at`` (``None`` = leave the stored value unchanged)."""
    spawn: bool
    fire_at: datetime | None
    watermark: datetime | None


def evaluate_fire(
    raw: dict | None, *, last_fired: datetime | None, now: datetime,
    grace: timedelta = DEFAULT_CATCHUP_GRACE,
) -> FireDecision:
    """Whether a cron schedule should fire a session at ``now`` (#603 dispatch).

    Pure + deterministic — the caller passes ``now`` and the persisted
    ``last_fired`` watermark, so this is unit-testable with no clock or DB. The
    rules, in order:

    * No cron in the schedule → never fires. (An active-hours-only schedule gates
      a *continuous* automation; it does not fire discrete sessions.)
    * ``last_fired is None`` → establish a watermark at ``now`` WITHOUT firing, so
      a freshly-created job never backfires cron minutes from before ccswitch knew
      about it.
    * Otherwise take the most recent cron minute in ``(last_fired, now]`` that is
      no older than ``grace``:
        * found → spawn and advance the watermark to that minute. A run missed
          while the daemon was down fires exactly once on recovery; several missed
          minutes coalesce to the latest (no replay burst).
        * none → no fire, watermark unchanged. A run missed by more than ``grace``
          is dropped.

    Firing only strictly after ``last_fired`` (never equal) is what prevents a
    double-fire when several ticks land inside the same cron minute, and — because
    the watermark is persisted — across a daemon restart within that minute.
    """
    sched = raw if isinstance(raw, Schedule) else parse_schedule(raw)
    if sched.cron is None:
        return FireDecision(spawn=False, fire_at=None, watermark=None)
    if last_fired is None:
        return FireDecision(spawn=False, fire_at=None, watermark=now)
    cron = parse_cron(sched.cron)
    floor = max(last_fired, now - grace)
    cursor = now.replace(second=0, microsecond=0)
    while cursor > last_fired and cursor >= floor:
        if cron.matches(cursor):
            return FireDecision(spawn=True, fire_at=cursor, watermark=cursor)
        cursor -= timedelta(minutes=1)
    return FireDecision(spawn=False, fire_at=None, watermark=None)


def next_runs(raw: dict | None, now: datetime, count: int = 3) -> list[datetime]:
    """Next ``count`` cron fire times strictly after ``now`` (minute resolution).

    Returns [] when the schedule has no cron. Searches up to ~366 days ahead, so
    a schedule that can never fire (e.g. Feb 30) returns fewer than ``count``."""
    sched = parse_schedule(raw)
    if sched.cron is None:
        return []
    cron = parse_cron(sched.cron)
    out: list[datetime] = []
    # Start at the next whole minute after now.
    cursor = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
    limit = now + timedelta(days=366)
    while cursor <= limit and len(out) < count:
        if cron.matches(cursor):
            out.append(cursor)
        cursor += timedelta(minutes=1)
    return out


# Human-readable schedule presets the UI offers (raw values it POSTs verbatim).
PRESETS: dict[str, dict] = {
    "always": {},
    "weekdays-9-18": {"activeHours": {"start": "09:00", "end": "18:00",
                                      "days": [0, 1, 2, 3, 4]}},
    "daily-8am": {"cron": "0 8 * * *"},
    "hourly": {"cron": "0 * * * *"},
    "every-15m": {"cron": "*/15 * * * *"},
}
