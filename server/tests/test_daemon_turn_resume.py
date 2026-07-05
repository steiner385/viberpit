import asyncio
import time
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


def test_status_done_and_complete_close_turn_when_no_turnend():
    """Regression: the genuinely terminal statuses close the turn as a fallback for
    the drain exception path, which settles to done/complete/needs_attention WITHOUT
    ever emitting a turnEnd (TurnEnd is only produced from the SDK ResultMessage)."""
    for terminal in ("done", "complete"):
        app = create_daemon_app()

        async def run():
            s = "sess2b"
            await app.state.publish(s, {"type": "user", "text": "hi"})
            await app.state.publish(s, {"type": "status", "state": terminal})
            assert app.state.turn_open_for(s) is False

        asyncio.run(run())


def test_transient_idle_status_does_not_close_open_turn():
    """`idle` is derive_status's never-ran sentinel (turns_completed == 0), NOT a
    turn-completion signal — recovery/reset paths (interrupt of the first turn, a
    rehydrated/rebuilt driver) emit it while a turn is still logically in flight. It
    must NOT close the turn: doing so drops the resume anchor mid-turn (skips the
    ring's in-progress events) AND nulls turn_started_at, re-enabling the positional
    trailing-turn amputation #482 fixed."""
    app = create_daemon_app()

    async def run():
        s = "sess_idle"
        await app.state.publish(s, {"type": "user", "text": "hi"})       # seq 0 -> open
        ts = app.state.turn_started_at(s)
        await app.state.publish(s, {"type": "textDelta", "text": "a"})   # seq 1
        # A transient idle arrives mid-turn — turn stays open, anchors intact.
        await app.state.publish(s, {"type": "status", "state": "idle"})  # seq 2
        assert app.state.turn_open_for(s) is True
        assert app.state.turn_started_at(s) == ts
        assert app.state.resume_since(s) == -1   # still anchored to turn_start_seq(0) - 1
        # A real turnEnd still closes it.
        await app.state.publish(s, {"type": "turnEnd", "turnId": "t"})   # seq 3
        assert app.state.turn_open_for(s) is False
        assert app.state.turn_started_at(s) is None
        assert app.state.resume_since(s) == app.state.head_seq(s)

    asyncio.run(run())


def test_turn_failure_closes_turn_and_clears_anchors():
    """turnFailure is an end-of-turn EVENT (the recoverable-failure settle: rate_limit
    / auth_401 / turn_dropped). It closes the turn just like turnEnd."""
    app = create_daemon_app()

    async def run():
        s = "sess_fail"
        await app.state.publish(s, {"type": "user", "text": "hi"})       # seq 0 -> open
        assert app.state.turn_open_for(s) is True
        assert app.state.turn_started_at(s) is not None
        await app.state.publish(
            s, {"type": "turnFailure", "kind": "rate_limit", "account": "a"})
        assert app.state.turn_open_for(s) is False
        assert app.state.turn_started_at(s) is None
        assert app.state.resume_since(s) == app.state.head_seq(s)

    asyncio.run(run())


def test_turn_started_at_tracks_open_turn():
    """turn_started_at(stream) mirrors turn_open_for: a wall-clock epoch while the
    turn is open (recorded when the `user` event was published), None otherwise.
    ws.py hands it to replay_window so the incomplete-turn drop can tell the
    in-progress turn's disk user entry apart from a PREVIOUS turn's (resume-lag
    amputation bug)."""
    app = create_daemon_app()

    async def run():
        s = "sess3"
        assert app.state.turn_started_at(s) is None      # nothing published yet
        t0 = time.time()
        await app.state.publish(s, {"type": "user", "text": "hi"})
        ts = app.state.turn_started_at(s)
        assert isinstance(ts, float) and t0 <= ts <= time.time()
        await app.state.publish(s, {"type": "textDelta", "text": "x"})
        assert app.state.turn_started_at(s) == ts        # stable across the turn
        await app.state.publish(s, {"type": "turnEnd", "turnId": "t"})
        assert app.state.turn_started_at(s) is None      # closed → no open turn

    asyncio.run(run())


def test_turn_started_at_cleared_by_terminal_status_and_evict():
    app = create_daemon_app()

    async def run():
        s = "sess4"
        await app.state.publish(s, {"type": "user", "text": "hi"})
        assert app.state.turn_started_at(s) is not None
        # turn_open can be flipped False by a terminal status with no turnEnd — the
        # accessor must go None with it, not leak the stale start time.
        await app.state.publish(s, {"type": "status", "state": "needs_attention"})
        assert app.state.turn_started_at(s) is None
        # A new turn re-arms it …
        await app.state.publish(s, {"type": "user", "text": "again"})
        assert app.state.turn_started_at(s) is not None
        # … and evict() clears all per-stream turn state.
        app.state.evict(s)
        assert app.state.turn_started_at(s) is None

    asyncio.run(run())
