"""Test that _record_to_dict exposes liveness + lastConfirmedAliveAt on session payloads.

Liveness is DERIVED at the edge from the owning host's heartbeat snapshot
(spec 002 FR-103). These plain unit tests construct a HeartbeatState directly
(no DB needed) and call _record_to_dict with a synthetic heartbeats dict —
mirroring the test_sessions_serialize_pending.py pattern.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from server.api.sessions import _record_to_dict
from server.sessions.types import SessionRecord
from server.sessions.host_heartbeat import HeartbeatState


def _make_record(**kw):
    d = dict(id="s1", name="t", account="a", claude_session_id="c1", status="in_progress",
             created_at=None, detached=False, cols=80, rows=24, cwd="/tmp")
    d.update(kw)
    return SessionRecord(**d)


def _hb(host, age_s):
    beat = datetime.utcnow() - timedelta(seconds=age_s)
    return {host: HeartbeatState(host=host, beat_at=beat, fence=1, epoch=None, age_s=age_s)}


def test_unreachable_when_owning_host_stale():
    """A session on a host whose heartbeat is 999 s old → unreachable."""
    rec = _make_record(host="w1")
    d = _record_to_dict(rec, heartbeats=_hb("w1", 999))
    assert d["liveness"] == "unreachable"
    assert d["lastConfirmedAliveAt"] is not None, "should carry the last beat timestamp"
    # Additive: pre-existing fields still present
    assert d["status"] == "in_progress"


def test_fresh_when_owning_host_recent():
    """A session on a host whose heartbeat is 5 s old (within 2x interval) → fresh."""
    rec = _make_record(host="w1")
    d = _record_to_dict(rec, heartbeats=_hb("w1", 5))
    assert d["liveness"] == "fresh"
    assert d["lastConfirmedAliveAt"] is not None, "fresh with a row → timestamp present"
    assert d["status"] == "in_progress"


def test_host_none_is_fresh_null():
    """host=None (primary/in-process session) → always fresh, no timestamp.

    Per spec NFR-3 + ruling R2: absence is never evidence of death;
    in-process sessions (NULL host) never demote in Phase 1.
    """
    rec = _make_record(host=None)
    # Even if w1 is stale, host=None sessions must not demote
    d = _record_to_dict(rec, heartbeats=_hb("w1", 999))
    assert d["liveness"] == "fresh"
    assert d["lastConfirmedAliveAt"] is None
    assert d["status"] == "in_progress"


def test_missing_row_is_fresh_null():
    """Session whose host has no heartbeat row → fresh/null (rollout rule NFR-3).

    An absent row is NEVER evidence of death — a worker that hasn't registered
    yet (e.g. rolling deploy) must not be diagnosed as dead.
    """
    rec = _make_record(host="w1")
    # Empty snapshot: w1 has no row
    d = _record_to_dict(rec, heartbeats={})
    assert d["liveness"] == "fresh"
    assert d["lastConfirmedAliveAt"] is None
    assert d["status"] == "in_progress"
