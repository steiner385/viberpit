from server.sessions.types import SessionRecord


def _rec(**kw) -> SessionRecord:
    base = dict(id="i", name="n", account="a", claude_session_id="c",
                cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_usage_unverified_defaults_false():
    assert _rec().usage_unverified is False


def test_usage_unverified_round_trips():
    rec = _rec(usage_unverified=True)
    assert rec.to_dict()["usageUnverified"] is True
    assert SessionRecord.from_dict(rec.to_dict()).usage_unverified is True


def test_from_dict_missing_key_defaults_false():
    d = _rec().to_dict()
    d.pop("usageUnverified", None)
    assert SessionRecord.from_dict(d).usage_unverified is False
