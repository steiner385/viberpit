"""QuotaSnapshot must survive the daemon RPC boundary intact.

``RemoteOrchestrator.usage()`` rebuilds a snapshot from ``to_dict()`` output the
daemon produced, so a lossy field here shows up as wrong quota numbers on the
edge — not as an exception.
"""
import json
from datetime import datetime, timezone

from ccswitch.quota import QUOTA_BLOB_VERSION, QuotaSnapshot, anthropic_snapshot, copilot_snapshot

NOW = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)


def test_usage_round_trip_with_datetimes():
    u = anthropic_snapshot(
        session_pct=42.0,
        session_resets_at=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
        weekly_pct=18.0, weekly_resets_at=None, extra_usage_enabled=True,
        fetched_at=NOW)
    d = u.to_dict()
    assert d["v"] == QUOTA_BLOB_VERSION
    assert d["windows"][0]["resets_at"] == "2026-06-22T10:00:00+00:00"
    assert d["windows"][1]["resets_at"] is None
    assert QuotaSnapshot.from_dict(d) == u


def test_copilot_round_trip_keeps_exactly_one_window():
    u = copilot_snapshot(20.0, NOW, fetched_at=NOW)
    back = QuotaSnapshot.from_dict(u.to_dict())
    assert back == u
    assert [w.key for w in back.windows] == ["premium"]


def test_usage_to_dict_is_json_safe():
    json.dumps(anthropic_snapshot(1.0, None, 2.0, None, False, fetched_at=NOW).to_dict())
    json.dumps(copilot_snapshot(1.0, None, fetched_at=NOW).to_dict())
