from server.sessions.events import SessionMeta


def test_session_meta_wire_carries_account_and_pin():
    w = SessionMeta(model="opus", session_id="s1", account="acct-a", account_pinned=True).to_wire()
    assert w == {"type": "sessionMeta", "model": "opus", "sessionId": "s1",
                 "account": "acct-a", "accountPinned": True}


def test_session_meta_defaults_keep_account_optional():
    w = SessionMeta(model="opus", session_id="s1").to_wire()
    assert w["account"] is None
    assert w["accountPinned"] is False
