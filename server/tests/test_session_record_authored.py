from server.sessions.types import SessionRecord


def _rec(**kw):
    base = dict(id="s1", name="n", account="a", claude_session_id="",
                cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_authored_branches_defaults_empty():
    assert _rec().authored_branches == []


def test_authored_branches_roundtrips_through_dict():
    rec = _rec(authored_branches=["feat/one", "feat/two"])
    again = SessionRecord.from_dict(rec.to_dict())
    assert again.authored_branches == ["feat/one", "feat/two"]


def test_from_dict_tolerates_missing_authored_branches():
    d = _rec().to_dict()
    del d["authoredBranches"]
    assert SessionRecord.from_dict(d).authored_branches == []
