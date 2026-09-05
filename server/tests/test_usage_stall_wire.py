"""pausedReason / pausedUntil / usageStalled reach BOTH serializers and round-trip."""
from __future__ import annotations

import datetime as _dt

from server.api.sessions import _record_to_dict
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord
from server.sessions.usage_stall import USAGE_STALL_REASONS, is_usage_stalled


def _rec(**kw) -> SessionRecord:
    base = dict(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c",
                cols=80, rows=24, cwd="/tmp", status="idle")
    base.update(kw)
    return SessionRecord(**base)  # type: ignore[arg-type]


def test_stall_reasons_are_the_three_usage_tokens():
    assert USAGE_STALL_REASONS == frozenset(
        {"all_accounts_busy", "rotation_cap", "pinned_account_limited"})


def test_is_usage_stalled_table():
    assert is_usage_stalled("limited", None) is True
    assert is_usage_stalled("needs_attention", "all_accounts_busy") is True
    assert is_usage_stalled("needs_attention", "rotation_cap") is True
    assert is_usage_stalled("needs_attention", "crash_cap") is False
    assert is_usage_stalled("needs_attention", None) is False
    assert is_usage_stalled("idle", "all_accounts_busy") is False


def test_record_to_dict_emits_stall_fields():
    until = _dt.datetime(2026, 9, 4, 16, 10, 0)
    d = _record_to_dict(_rec(status="needs_attention", paused_reason="all_accounts_busy",
                             paused_until=until))
    assert d["pausedReason"] == "all_accounts_busy"
    assert d["pausedUntil"] == "2026-09-04T16:10:00+00:00"
    assert d["usageStalled"] is True


def test_record_to_dict_defaults():
    d = _record_to_dict(_rec())
    assert d["pausedReason"] is None
    assert d["pausedUntil"] is None
    assert d["usageStalled"] is False


def test_to_dict_from_dict_round_trip():
    until = _dt.datetime(2026, 9, 4, 16, 10, 0)
    r = _rec(status="limited", paused_reason="pinned_account_limited", paused_until=until)
    d = r.to_dict()
    assert d["usageStalled"] is True
    back = SessionRecord.from_dict(d)
    assert back.paused_reason == "pinned_account_limited"
    assert back.paused_until is not None
    assert back.paused_until.replace(tzinfo=None) == until
