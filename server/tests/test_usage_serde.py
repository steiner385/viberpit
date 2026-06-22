from datetime import datetime, timezone
from ccswitch.oauth import Usage


def test_usage_round_trip_with_datetimes():
    u = Usage(session_pct=42.0,
              session_resets_at=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
              weekly_pct=18.0, weekly_resets_at=None, extra_usage_enabled=True)
    d = u.to_dict()
    assert d["sessionResetsAt"] == "2026-06-22T10:00:00+00:00"
    assert d["weeklyResetsAt"] is None
    assert Usage.from_dict(d) == u


def test_usage_to_dict_is_json_safe():
    import json
    json.dumps(Usage(1.0, None, 2.0, None, False).to_dict())
