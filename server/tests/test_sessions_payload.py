# server/tests/test_sessions_payload.py
import datetime as _dt

from server.api.sessions import _record_to_dict
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _rec(**kw):
    r = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c",
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


def test_record_to_dict_emits_engine_native_identity_and_reasoning_effort():
    d = _record_to_dict(_rec(reasoning_effort="high"), repo=None)
    assert d["engine"] == "claude_code"
    assert d["engineSessionId"] == "c"
    assert d["reasoningEffort"] == "high"


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


# ---------------------------------------------------------------------------
# Restart-interruption fields: interruptedFromStatus / resumable
#
# _record_to_dict is an explicit allowlist and is the BROWSER serializer —
# SessionRecord.to_dict (server/sessions/types.py) is the separate daemon→edge
# RPC one. Adding a field to only one of them ships a dead feature: the web
# client's InterruptedAskNotice read interruptedFromStatus and always got
# undefined, and #938's "Resume interrupted (N)" button always counted 0.
# ---------------------------------------------------------------------------

def test_record_to_dict_emits_interrupted_from_status_for_a_dropped_ask():
    rec = _rec(interrupted_by_restart=True, interrupted_from_status="waiting_input")
    d = _record_to_dict(rec)
    assert d["interruptedFromStatus"] == "waiting_input"
    # An awaiting-human status is NOT dropped work, so it must not be resumable.
    assert d["resumable"] is False


def test_record_to_dict_marks_dropped_work_resumable():
    rec = _rec(interrupted_by_restart=True, interrupted_from_status="in_progress")
    d = _record_to_dict(rec)
    assert d["interruptedFromStatus"] == "in_progress"
    assert d["resumable"] is True


def test_record_to_dict_interruption_fields_default_clean():
    d = _record_to_dict(_rec())
    assert d["interruptedFromStatus"] is None
    assert d["resumable"] is False
