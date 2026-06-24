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
