# server/tests/test_sessions_payload.py
import datetime as _dt

from server.api.sessions import _record_to_dict
from server.sessions.types import SessionRecord


def _rec(**kw):
    r = SessionRecord(id="s1", name="n", account="a", claude_session_id="c",
                      cols=80, rows=24, cwd="/x")
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_record_to_dict_includes_suggestions():
    d = _record_to_dict(_rec(), repo=None, suggestions=["Run the tests"])
    assert d["latestSuggestions"] == ["Run the tests"]


def test_record_to_dict_defaults_empty_suggestions():
    d = _record_to_dict(_rec(), repo=None)
    assert d["latestSuggestions"] == []


# ---------------------------------------------------------------------------
# autopilotCooldownUntil derived field
# ---------------------------------------------------------------------------

def test_cooldown_until_present_within_window():
    out = _record_to_dict(_rec(last_user_interaction_at=_dt.datetime.utcnow()), defer_seconds=120)
    assert out.get("autopilotCooldownUntil") is not None


def test_cooldown_until_absent_after_window():
    rec = _rec(last_user_interaction_at=_dt.datetime.utcnow() - _dt.timedelta(seconds=200))
    assert _record_to_dict(rec, defer_seconds=120).get("autopilotCooldownUntil") is None


def test_cooldown_until_absent_when_paused():
    rec = _rec(last_user_interaction_at=_dt.datetime.utcnow(), autopilot_paused=True)
    assert _record_to_dict(rec, defer_seconds=120).get("autopilotCooldownUntil") is None


def test_cooldown_until_absent_when_defer_none():
    rec = _rec(last_user_interaction_at=_dt.datetime.utcnow())
    assert _record_to_dict(rec, defer_seconds=None).get("autopilotCooldownUntil") is None


def test_cooldown_until_absent_when_never_interacted():
    assert _record_to_dict(_rec(), defer_seconds=120).get("autopilotCooldownUntil") is None


# ---------------------------------------------------------------------------
# Daemon-mode: aware datetime from RPC round-trip must not raise TypeError
# ---------------------------------------------------------------------------

def test_cooldown_until_handles_aware_datetime_from_rpc():
    """After an RPC round-trip, last_user_interaction_at is tz-aware.
    The comparison must NOT raise TypeError (aware vs naive). Regression guard."""
    rec = _rec(last_user_interaction_at=_dt.datetime.now(_dt.timezone.utc))
    out = _record_to_dict(rec, defer_seconds=120)   # must not raise
    assert out.get("autopilotCooldownUntil") is not None
