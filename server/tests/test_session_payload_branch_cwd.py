from server.api.sessions import _record_to_dict
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _rec(**kw):
    base = dict(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_payload_includes_branch_and_cwd():
    d = _record_to_dict(_rec(branch="feat/x", cwd="/w"), repo=None)
    assert d["branch"] == "feat/x"
    assert d["cwd"] == "/w"


def test_payload_branch_null_when_absent():
    d = _record_to_dict(_rec(), repo=None)
    assert d["branch"] is None


def test_remote_roundtrip_keeps_branch_and_cwd():
    r = SessionRecord.from_dict(_rec(branch="feat/x", cwd="/w").to_dict())
    d = _record_to_dict(r, repo=None)
    assert (d["branch"], d["cwd"]) == ("feat/x", "/w")


def test_payload_empty_branch_is_null():
    d = _record_to_dict(_rec(branch=""), repo=None)
    assert d["branch"] is None


def test_payload_empty_cwd_is_null():
    d = _record_to_dict(_rec(cwd=""), repo=None)
    assert d["cwd"] is None
