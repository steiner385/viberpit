"""Round-trip tests for SessionRecord.to_dict / from_dict serialization."""
from typing import Any

import pytest

from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _make_record(**kwargs) -> SessionRecord:
    defaults: dict[str, Any] = dict(
        id="s1",
        name="n",
        account="a",
        engine=AgentEngine.CLAUDE_CODE,
        engine_session_id="cs1",
        cols=80,
        rows=24,
        cwd="/tmp",
    )
    defaults.update(kwargs)
    return SessionRecord(**defaults)


def test_pr_resolution_roundtrips_through_dict():
    rec = _make_record()
    rec.pr_resolution = "cancelled: dropped the change"
    rec.pr_resolution_source = "marker"
    d = rec.to_dict()
    assert d["prResolution"] == "cancelled: dropped the change"
    assert d["prResolutionSource"] == "marker"
    back = SessionRecord.from_dict(d)
    assert back.pr_resolution == "cancelled: dropped the change"
    assert back.pr_resolution_source == "marker"


def test_pr_resolution_defaults_none():
    rec = _make_record()
    assert rec.pr_resolution is None
    assert rec.pr_resolution_source is None
    assert SessionRecord.from_dict(rec.to_dict()).pr_resolution is None


def test_old_daemon_wire_shape_deserializes_during_rolling_upgrade():
    wire = _make_record().to_dict()
    wire.pop("engine")
    native_id = wire.pop("engineSessionId")
    wire["claudeSessionId"] = native_id

    record = SessionRecord.from_dict(wire)

    assert record.engine is AgentEngine.CLAUDE_CODE
    assert record.engine_session_id == "cs1"


def test_new_daemon_keeps_old_edge_session_id_alias_for_claude():
    wire = _make_record(engine_session_id="cs1").to_dict()

    assert wire["engineSessionId"] == "cs1"
    assert wire["claudeSessionId"] == "cs1"


def test_new_daemon_does_not_mislabel_codex_id_as_claude():
    wire = _make_record(
        engine=AgentEngine.CODEX,
        engine_session_id="codex-thread",
    ).to_dict()

    assert wire["engineSessionId"] == "codex-thread"
    assert wire["claudeSessionId"] is None


def test_explicit_null_engine_is_not_treated_as_legacy_omission():
    wire = _make_record().to_dict()
    wire["engine"] = None

    with pytest.raises(ValueError, match="agent engine is required"):
        SessionRecord.from_dict(wire)
