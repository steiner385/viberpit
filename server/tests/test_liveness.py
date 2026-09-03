"""Unit tests for pure liveness derivation (spec 002 FR-102, Task 3)."""
from datetime import datetime, timedelta
import pytest
from server.sessions.liveness import derive_liveness

NOW = datetime(2026, 9, 2, 12, 0, 0)


@pytest.mark.parametrize("age_s,declared,expected", [
    (0, False, "fresh"),
    (19.9, False, "fresh"),          # <= 2x interval
    (20.1, False, "stale"),
    (299.9, False, "stale"),
    (300.1, False, "unreachable"),
    (300.1, True, "dead"),
    (-30, False, "fresh"),           # writer clock ahead of reader: never punish skew
])
def test_thresholds(age_s, declared, expected):
    beat = NOW - timedelta(seconds=age_s)
    lv = derive_liveness(beat, NOW, declared_dead=declared)
    assert lv.state == expected
    assert lv.last_confirmed_alive_at == beat


def test_missing_row_is_fresh_unknown():
    # NFR-3: absence of evidence is not evidence of death (rollout safety).
    lv = derive_liveness(None, NOW)
    assert lv.state == "fresh" and lv.last_confirmed_alive_at is None
