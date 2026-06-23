from server.ipc import protocol as p


def test_rpc_response_ok_shape():
    assert p.rpc_response_ok("id1", {"pong": True}) == {"id": "id1", "result": {"pong": True}}


def test_rpc_response_err_shape():
    assert p.rpc_response_err("id2", "ValueError", "bad") == {
        "id": "id2", "error": {"type": "ValueError", "message": "bad"}}


def test_event_frame_shape():
    assert p.event_frame("s1", {"type": "tick", "i": 0}, 0) == {
        "type": "event", "stream": "s1", "event": {"type": "tick", "i": 0}, "seq": 0}


def test_subscribe_unsubscribe_frames():
    assert p.subscribe_frame("s1") == {"type": "subscribe", "stream": "s1", "since": None}
    assert p.unsubscribe_frame("s1") == {"type": "unsubscribe", "stream": "s1"}
