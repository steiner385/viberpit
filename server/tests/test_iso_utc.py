"""Wire timestamps must be UTC-aware ISO-8601.

Regression: DB DateTime columns store *naive* UTC, and a bare `.isoformat()` drops
the offset. A browser parses an offset-less date-time as *local* time (ECMAScript
rule), so for any client behind UTC the instant lands in the future and the
dashboard's relative-time clamp pins every session card to "active 0s ago".
`iso_utc` normalizes every wire timestamp to carry an explicit `+00:00` offset.
"""
from datetime import datetime, timedelta, timezone

from server.time_iso import iso_utc


def test_naive_datetime_is_tagged_utc():
    # The shape every DB column produces: naive, implicitly UTC.
    s = iso_utc(datetime(2026, 6, 22, 11, 58, 0))
    assert s == "2026-06-22T11:58:00+00:00"
    # Parses back to the SAME instant (not shifted by the local zone).
    assert datetime.fromisoformat(s) == datetime(2026, 6, 22, 11, 58, 0, tzinfo=timezone.utc)


def test_aware_utc_datetime_preserved():
    s = iso_utc(datetime(2026, 6, 22, 11, 58, 0, tzinfo=timezone.utc))
    assert s == "2026-06-22T11:58:00+00:00"


def test_aware_non_utc_datetime_converted_to_utc():
    eastern = timezone(timedelta(hours=-4))
    s = iso_utc(datetime(2026, 6, 22, 7, 58, 0, tzinfo=eastern))  # 11:58 UTC
    assert s == "2026-06-22T11:58:00+00:00"


def test_none_passthrough():
    assert iso_utc(None) is None
