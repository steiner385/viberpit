from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


def test_push_subscription_columns():
    from server.db.models import PushSubscription
    cols = PushSubscription.__table__.columns
    assert "endpoint" in cols and cols["endpoint"].primary_key
    for name in ("p256dh", "auth", "identity", "created_at"):
        assert name in cols
    # identity + created_at must be nullable (init_db backfills; no migration).
    assert cols["identity"].nullable
    assert cols["created_at"].nullable
