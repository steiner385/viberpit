"""Test that _record_to_dict exposes pendingModel in the wire-format dict.

`pending_model` (string | null) is the approval-hold model parked by the
model-approval gate — distinct from the pre-existing boolean `modelPending`
("switch after this turn").
"""
from __future__ import annotations

from server.api.sessions import _record_to_dict
from server.sessions.types import SessionRecord


def _make_record(**kwargs):
    defaults = dict(
        id="s1",
        name="test",
        account="acct-a",
        claude_session_id="cs1",
        status="idle",
        created_at=None,
        detached=False,
        cols=80,
        rows=24,
        cwd="/tmp",
    )
    defaults.update(kwargs)
    return SessionRecord(**defaults)


def test_pending_model_serialized():
    rec = _make_record(status="pending_approval", pending_model="fable")
    d = _record_to_dict(rec)
    assert d["status"] == "pending_approval"
    assert d["pendingModel"] == "fable"


def test_pending_model_null_when_not_set():
    rec = _make_record()
    d = _record_to_dict(rec)
    assert d["pendingModel"] is None


def test_pending_model_does_not_shadow_model_pending():
    """pendingModel (approval-hold model string) must co-exist with the separate
    boolean modelPending ("switch after this turn") — they are different fields."""
    rec = _make_record(model="opus", pending_model="fable")
    d = _record_to_dict(rec)
    assert d["model"] == "opus"
    assert d["pendingModel"] == "fable"
    # modelPending is still present and independent
    assert "modelPending" in d
