"""UTC-aware ISO-8601 serialization for wire timestamps.

DB ``DateTime`` columns store *naive* UTC (legacy ``datetime.utcnow`` defaults),
while some in-memory paths carry tz-aware UTC. Serializing a naive datetime with a
bare ``.isoformat()`` drops the offset, so a browser parses the string as *local*
time — ECMAScript treats an offset-less date-time as local. For any client behind
UTC the instant lands in the future and the dashboard's relative-time clamp pins
every card to "active 0s ago". Route every wire timestamp through ``iso_utc`` so the
offset is always explicit and every consumer parses the same instant.
"""
from __future__ import annotations

from datetime import datetime, timezone


def iso_utc(dt: datetime | None) -> str | None:
    """ISO-8601 string with an explicit UTC offset, or ``None``.

    A naive datetime is assumed to be UTC (everything we persist is). An aware
    datetime is converted to UTC. ``None`` passes through so callers stay terse.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def parse_iso_utc(value: str | None) -> datetime | None:
    """The read direction of ``iso_utc``: an ISO-8601 string → aware UTC datetime.

    Tolerates GitHub's trailing ``Z`` form (which ``datetime.fromisoformat``
    rejects before Python 3.11) and offset-less strings, which are read as UTC for
    the same reason ``iso_utc`` writes them that way — everything this app
    persists is UTC. Returns ``None`` for ``None``, an empty string, or anything
    unparseable, so a single corrupt row degrades to a skipped sample instead of
    crashing the reader that found it.

    Lives here, next to ``iso_utc``, rather than as another private
    ``fromisoformat(s.replace("Z", "+00:00"))`` copy: this module is the app's
    ISO boundary (constitution VI). Several older ``server/pipeline`` and
    ``server/github`` modules still carry their own private variants returning
    epoch seconds or milliseconds; consolidating those is a separate change.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
