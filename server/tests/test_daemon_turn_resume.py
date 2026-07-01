import asyncio
from server.ipc.daemon import create_daemon_app


def test_publish_tracks_turn_start_and_open_state():
    app = create_daemon_app()

    async def run():
        s = "sess"
        await app.state.publish(s, {"type": "user", "text": "hi"})       # seq 0
        await app.state.publish(s, {"type": "textDelta", "text": "a"})   # seq 1
        await app.state.publish(s, {"type": "textDelta", "text": "b"})   # seq 2
        assert app.state.turn_open_for(s) is True
        # mid-turn: resume from just before the user message → replays the whole turn
        assert app.state.resume_since(s) == -1   # turn_start_seq(0) - 1
        await app.state.publish(s, {"type": "turnEnd", "turnId": "t"})   # seq 3
        assert app.state.turn_open_for(s) is False
        assert app.state.resume_since(s) == app.state.head_seq(s)        # from-now once closed

    asyncio.run(run())


def test_status_terminal_closes_turn_when_no_turnend():
    app = create_daemon_app()

    async def run():
        s = "sess2"
        await app.state.publish(s, {"type": "user", "text": "hi"})       # seq 0 -> open
        await app.state.publish(s, {"type": "textDelta", "text": "x"})   # seq 1
        await app.state.publish(s, {"type": "status", "state": "needs_attention"})
        assert app.state.turn_open_for(s) is False

    asyncio.run(run())
