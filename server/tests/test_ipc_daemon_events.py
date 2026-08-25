import json
from fastapi.testclient import TestClient
from server.ipc.daemon import create_daemon_app


def test_subscribe_receives_published_events():
    app = create_daemon_app()
    with TestClient(app) as c:
        with c.websocket_connect("/events") as ws:
            ws.send_text(json.dumps({"type": "subscribe", "stream": "s1"}))
            c.post("/rpc/start_demo", json={"stream": "s1", "n": 3})
            got = [json.loads(ws.receive_text()) for _ in range(3)]
    assert [f["event"]["i"] for f in got] == [0, 1, 2]
    assert all(f["type"] == "event" and f["stream"] == "s1" for f in got)


def test_echo_round_trips_to_subscriber():
    app = create_daemon_app()
    with TestClient(app) as c:
        with c.websocket_connect("/events") as ws:
            ws.send_text(json.dumps({"type": "subscribe", "stream": "s2"}))
            c.post("/rpc/echo", json={"stream": "s2", "text": "hi"})
            frame = json.loads(ws.receive_text())
    assert frame == {"type": "event", "stream": "s2", "event": {"type": "echo", "text": "hi"}, "seq": 0}


def test_unsubscribed_stream_not_delivered():
    app = create_daemon_app()
    with TestClient(app) as c:
        with c.websocket_connect("/events") as ws:
            ws.send_text(json.dumps({"type": "subscribe", "stream": "mine"}))
            c.post("/rpc/echo", json={"stream": "other", "text": "x"})
            c.post("/rpc/echo", json={"stream": "mine", "text": "y"})
            frame = json.loads(ws.receive_text())
    # First (and only) delivered frame is for "mine", proving "other" was filtered.
    assert frame["stream"] == "mine" and frame["event"]["text"] == "y"


def test_close_events_connections_rpc_cancels_writer_tasks():
    """close_events_connections must report the number of writer tasks it cancelled.

    The deploy controller calls this before restarting the daemon (issue #740) so the
    long-lived /events WebSocket doesn't hold up uvicorn's graceful shutdown.
    """
    app = create_daemon_app()
    with TestClient(app) as c:
        # One active /events connection.
        with c.websocket_connect("/events"):
            result = c.post("/rpc/close_events_connections", json={})
            assert result.status_code == 200
            body = result.json()
            assert body["result"]["closed"] >= 1

    # No active connections — returns 0 without error.
    with TestClient(app) as c:
        result = c.post("/rpc/close_events_connections", json={})
        assert result.status_code == 200
        assert result.json()["result"]["closed"] == 0
