"""Unit tests for pure liveness derivation (spec 002 FR-102, Task 3)."""
from datetime import datetime, timedelta
import pytest
from server.sessions.liveness import (
    DEAD_AFTER_DEFAULT_S,
    DEAD_AFTER_ENV,
    HEARTBEAT_INTERVAL_DEFAULT_S,
    HEARTBEAT_INTERVAL_ENV,
    derive_liveness,
    heartbeat_interval_s,
    host_dead_after_s,
)

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


def test_snapshot_failed_is_unknown_not_fresh():
    # NFR-5: a FAILED snapshot (DB down at the edge) is not the same as a missing row.
    # We have no evidence at all, so degradation must be visible — never false-fresh.
    lv = derive_liveness(None, NOW, snapshot_failed=True)
    assert lv.state == "unknown" and lv.last_confirmed_alive_at is None
    # snapshot_failed wins even if a (stale) beat happens to be supplied.
    lv2 = derive_liveness(NOW - timedelta(seconds=5), NOW, snapshot_failed=True)
    assert lv2.state == "unknown"


# --------------------------------------------------------------------------- #
# Threshold knobs. These are read inside the session serializer (every session,
# every request, with no try/except above it) and at the top of the daemon's
# heartbeat loop, so a typo'd override must fall back — never raise. A raw
# float() here used to mean CCSWITCH_HOST_DEAD_AFTER_S=300s 500s the whole
# sessions page, and a bad interval killed the heartbeat task after one beat.
# --------------------------------------------------------------------------- #

def test_thresholds_default_when_unset(monkeypatch):
    monkeypatch.delenv(HEARTBEAT_INTERVAL_ENV, raising=False)
    monkeypatch.delenv(DEAD_AFTER_ENV, raising=False)
    assert heartbeat_interval_s() == HEARTBEAT_INTERVAL_DEFAULT_S
    assert host_dead_after_s() == DEAD_AFTER_DEFAULT_S


def test_thresholds_honour_valid_override(monkeypatch):
    monkeypatch.setenv(HEARTBEAT_INTERVAL_ENV, "5")
    monkeypatch.setenv(DEAD_AFTER_ENV, "60")
    assert heartbeat_interval_s() == 5.0
    assert host_dead_after_s() == 60.0


@pytest.mark.parametrize("bad", ["300s", "", "abc", "nan", "inf", "0", "-1"])
def test_thresholds_fall_back_on_bad_value_instead_of_raising(monkeypatch, bad):
    monkeypatch.setenv(HEARTBEAT_INTERVAL_ENV, bad)
    monkeypatch.setenv(DEAD_AFTER_ENV, bad)
    assert heartbeat_interval_s() == HEARTBEAT_INTERVAL_DEFAULT_S
    assert host_dead_after_s() == DEAD_AFTER_DEFAULT_S
