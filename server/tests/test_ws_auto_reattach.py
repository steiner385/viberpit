# server/tests/test_ws_auto_reattach.py
"""An attached viewer must converge without client action when its session is driven
from elsewhere (autopilot, queue drain, another device).

Two holes closed here:
  H1 — attaching to a DETACHED session replays history and subscribes to nothing
       (_handle_attach early-returns on rec.detached), so external activity was
       invisible until the user navigated away and back;
  H2 — when completion releases the driver mid-view, this socket's forwarding task
       ends silently and a later external resume was equally invisible.

The fix: the socket tracks which sessions it is *attached* to (independent of
live_tasks) and re-runs the attach path when a lifecycle status event arrives for an
attached session with no live forwarding task on this socket.
"""
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.ipc.client import DaemonUnavailable
from server.tests.test_ws_prompt_after_release import (
    _Harness, _IDENTITY_HEADERS, _drain_until, _release_finished, _wait_until,
)

_TRANSCRIPT_FIXTURE = str(Path(__file__).parent / "fixtures" / "transcript.jsonl")


def _with_history(orch) -> None:
    """Point every replay at a non-empty transcript.

    `replayReset` — the observable "this is a FRESH full replay, drop what you have"
    frame these tests assert on — is only emitted when the replay actually has
    history to send (or a live in-progress turn to re-supply); an empty transcript
    yields a bare `replayComplete`. The harness's FakeSDK session writes nothing to
    disk, so without this the auto re-attach would be indistinguishable from no
    re-attach at all. Same mechanism `test_ws.py` uses for its own replay assertions.
    """
    orch.transcript_path = lambda sid: _TRANSCRIPT_FIXTURE


async def _wedge_spawn(orch, session_id: str, released: list[float],
                       hold: float = 25.0) -> None:
    """Park a never-resolving spawn future for *session_id*.

    Two real orchestrator behaviours fall out of it, which together are exactly the
    state an auto re-attach wedges in: `_sync_detached` reports the record LIVE (a
    spawning session is not detached), so the re-attach is not skipped; and
    `ensure_driver` then waits on that future (SPAWN_WAIT_S), so `_handle_attach`
    blocks inside `_resume_driver_or_error` holding the session's attach lock. No
    patching, no fake timing.

    Self-releases after *hold* so a REGRESSION fails in bounded time instead of
    sitting on the 90 s `_RESUME_TIMEOUT`; the tests still release it explicitly.
    Every release stamps *released*, which is what lets a test assert ORDER ("this
    happened while the re-attach was still wedged") instead of a wall-clock budget
    that a loaded runner can blow.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    orch._spawning[session_id] = fut

    def _auto_release() -> None:
        if not fut.done():
            released.append(time.monotonic())
            fut.set_result(None)
        if orch._spawning.get(session_id) is fut:
            del orch._spawning[session_id]

    loop.call_later(hold, _auto_release)


async def _release_spawn(orch, session_id: str, released: list[float]) -> None:
    fut = orch._spawning.pop(session_id, None)
    if fut is not None and not fut.done():
        released.append(time.monotonic())
        fut.set_result(None)


def test_snapshot_viewer_converges_on_external_resume(tmp_path):
    """H1: attach AFTER the driver was released (a detached session — the everyday
    state after any completed task or daemon restart). Then resume + prompt the
    session purely server-side, as autopilot would. The viewer must receive a fresh
    replay WITHOUT sending a single frame."""
    with _Harness(tmp_path) as h:
        orch = h.orch
        with TestClient(h.app, raise_server_exceptions=False) as client:
            session = client.portal.call(orch.create_session, "auto-reattach-detached")
            sid = session.id
            _with_history(orch)
            orch.record(sid).auto_archive_opt_out_reason = "manual-unarchive"
            # Complete → completion-driver-release → the session is detached.
            client.portal.call(orch._persist_status, sid, "complete")
            assert sid not in orch._drivers
            assert _wait_until(lambda: _release_finished(orch))

            with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
                _drain_until(ws, until_type="replayComplete")
                # Detached attach = snapshot only: no live forwarding task exists.

                # EXTERNAL drive: resume the driver and deliver a prompt with no
                # frame from this socket (the autopilot shape).
                client.portal.call(orch.ensure_driver, sid)
                client.portal.call(orch.send_prompt, sid, "external work")
                assert _wait_until(
                    lambda: orch._last_prompt.get(sid) == "external work")

                # The resume's status transition must auto re-attach this socket:
                # a fresh unsolicited replayReset→…→replayComplete arrives.
                events = _drain_until(ws, until_type="replayComplete", timeout=8.0)
                resets = [e for e in events if e.get("type") == "replayReset"]
                assert resets, (
                    "an attached snapshot viewer must be re-attached when its "
                    f"session is driven externally; got {[e.get('type') for e in events]}")

            client.portal.call(orch.close_session, sid)


def test_released_mid_view_then_external_resume_converges(tmp_path):
    """H2: the viewer attached while LIVE; completion releases the driver (ending
    this socket's forwarding task silently); a later external resume must still
    reach the viewer via auto re-attach."""
    with _Harness(tmp_path) as h:
        orch = h.orch
        with TestClient(h.app, raise_server_exceptions=False) as client:
            session = client.portal.call(orch.create_session, "auto-reattach-live")
            sid = session.id
            _with_history(orch)
            orch.record(sid).auto_archive_opt_out_reason = "manual-unarchive"

            with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
                _drain_until(ws, until_type="replayComplete")
                assert sid in orch._drivers

                client.portal.call(orch._persist_status, sid, "complete")
                assert _wait_until(lambda: _release_finished(orch))
                time.sleep(0.3)   # let the forwarding task observe _STREAM_DONE and end
                # Drain the frames the completion itself produced so the next drain
                # unambiguously observes the post-resume replay.
                _drain_until(ws, until_type="status", timeout=2.0)

                client.portal.call(orch.ensure_driver, sid)
                client.portal.call(orch.send_prompt, sid, "second wind")
                assert _wait_until(
                    lambda: orch._last_prompt.get(sid) == "second wind")

                events = _drain_until(ws, until_type="replayComplete", timeout=8.0)
                assert any(e.get("type") == "replayReset" for e in events), (
                    "external resume after mid-view release must re-attach the viewer")

            client.portal.call(orch.close_session, sid)


def test_detach_stops_auto_reattach(tmp_path):
    """After the client detaches, external activity must NOT trigger a re-attach —
    detach removes the session from the socket's attached-set. Observed via an
    unknown-type terminator: no replayReset may appear at all."""
    with _Harness(tmp_path) as h:
        orch = h.orch
        with TestClient(h.app, raise_server_exceptions=False) as client:
            session = client.portal.call(orch.create_session, "auto-reattach-detach")
            sid = session.id
            _with_history(orch)
            orch.record(sid).auto_archive_opt_out_reason = "manual-unarchive"
            client.portal.call(orch._persist_status, sid, "complete")
            assert _wait_until(lambda: _release_finished(orch))

            with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
                _drain_until(ws, until_type="replayComplete")
                ws.send_text(json.dumps({"type": "detach", "sessionId": sid}))

                client.portal.call(orch.ensure_driver, sid)
                client.portal.call(orch.send_prompt, sid, "unwatched work")
                assert _wait_until(
                    lambda: orch._last_prompt.get(sid) == "unwatched work")
                time.sleep(0.5)   # window in which a buggy auto re-attach would fire

                # Terminator, deliberately NOT an attach: frames are answered in order,
                # so an unknown type bounds the drain with a top-level error (a
                # regression must FAIL, not hang) without emitting a replay of its own.
                # An attach terminator makes this test VACUOUS: _drain_until returns at
                # the FIRST replayComplete, which under the regression is the auto
                # re-attach's own — so its replayReset is miscounted as the terminator's
                # and `len(resets) == 1` holds whether or not detach still works.
                ws.send_text(json.dumps({"type": "bogus", "sessionId": sid}))
                events = _drain_until(ws, until_type="error", timeout=8.0)
                resets = [e for e in events if e.get("type") == "replayReset"]
                assert not resets, (
                    f"a detached socket must not auto re-attach; saw {len(resets)} "
                    "unsolicited replayReset frame(s)")

            client.portal.call(orch.close_session, sid)


def test_daemon_blip_during_auto_reattach_keeps_the_socket_open(tmp_path):
    """An auto re-attach is something the SOCKET decided to do, so nothing it hits may
    cost the user their connection. `_handle_attach`'s daemon-down path closes the
    socket (1013) — right for a user attach, catastrophic here: on a dashboard socket
    viewing six sessions, one status event during a 5 s daemon restart would close the
    socket for all six. The failure must be silent and the viewer must keep its
    snapshot."""
    with _Harness(tmp_path) as h:
        orch = h.orch
        with TestClient(h.app, raise_server_exceptions=False) as client:
            session = client.portal.call(orch.create_session, "auto-reattach-blip")
            sid = session.id
            _with_history(orch)
            orch.record(sid).auto_archive_opt_out_reason = "manual-unarchive"
            client.portal.call(orch._persist_status, sid, "complete")
            assert _wait_until(lambda: _release_finished(orch))

            with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
                _drain_until(ws, until_type="replayComplete")

                # The daemon goes away exactly as the session is driven externally,
                # so the status event's auto re-attach hits DaemonUnavailable.
                with patch.object(orch, "get_session",
                                  side_effect=DaemonUnavailable("daemon restarting")):
                    client.portal.call(orch.ensure_driver, sid)
                    client.portal.call(orch.send_prompt, sid, "during the blip")
                    assert _wait_until(
                        lambda: orch._last_prompt.get(sid) == "during the blip")
                    time.sleep(0.5)   # the auto re-attach fires and fails in here

                # Daemon back: the socket must still be open and usable. A closed one
                # makes this drain come back empty — an assertion, never a hang.
                ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
                events = _drain_until(ws, until_type="replayComplete", timeout=8.0)
                assert any(e.get("type") == "replayComplete" for e in events), (
                    "a daemon blip during an auto re-attach must not close the "
                    f"socket; got {[e.get('type') for e in events]}")

            client.portal.call(orch.close_session, sid)


def test_parked_detached_session_is_not_replayed_on_every_status_event(tmp_path):
    """A still-DETACHED session must be skipped, not re-attached.

    `_handle_attach`'s detached branch registers no live task, so the session stays
    attached-and-not-live forever and every later status event re-ran a full disk
    replay, pushing replayReset + the whole transcript window at a session that is not
    even running. Driver-less status writes are ordinary for paused / needs_attention /
    queued sessions, so a user parked on one watched its transcript reset on a loop.
    Re-attaching a detached session replays the exact snapshot the client already has,
    so there is nothing to converge to anyway."""
    with _Harness(tmp_path) as h:
        orch = h.orch
        with TestClient(h.app, raise_server_exceptions=False) as client:
            session = client.portal.call(orch.create_session, "auto-reattach-parked")
            sid = session.id
            _with_history(orch)
            orch.record(sid).auto_archive_opt_out_reason = "manual-unarchive"
            client.portal.call(orch._persist_status, sid, "complete")
            assert _wait_until(lambda: _release_finished(orch))

            with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
                _drain_until(ws, until_type="replayComplete")

                # Driver-less status churn, spaced past _AUTO_REATTACH_MIN_INTERVAL_S
                # so the rate floor is NOT what is being measured here.
                for status in ("paused", "needs_attention", "paused"):
                    client.portal.call(orch._persist_status, sid, status)
                    time.sleep(2.2)

                # Terminator, deliberately NOT an attach: frames are answered in
                # order, so an unknown type bounds the drain with a top-level error
                # (a regression must FAIL, not hang) without emitting a replay of its
                # own. Test 3's attach terminator would be worse than useless here —
                # _drain_until stops at the FIRST replayComplete, which under the bug
                # is the auto re-attach's, hiding the very frames being counted.
                ws.send_text(json.dumps({"type": "bogus", "sessionId": sid}))
                events = _drain_until(ws, until_type="error", timeout=8.0)
                resets = [e for e in events if e.get("type") == "replayReset"]
                assert not resets, (
                    f"a parked detached session must not be re-replayed; saw "
                    f"{len(resets)} unsolicited replayReset frame(s)")

            client.portal.call(orch.close_session, sid)


def test_wedged_auto_reattach_does_not_block_another_sessions_prompt(tmp_path):
    """Cross-session independence (#1180), which the auto re-attach must not undo.

    A socket-global attach lock put session B's user prompt behind session A's
    auto re-attach for as long as A's resume took (up to _RESUME_TIMEOUT, 90 s) — and
    unlike a slow user attach, this one fires with no user action at all."""
    with _Harness(tmp_path) as h:
        orch = h.orch
        with TestClient(h.app, raise_server_exceptions=False) as client:
            released: list[float] = []
            a = client.portal.call(orch.create_session, "auto-reattach-wedged")
            b = client.portal.call(orch.create_session, "unrelated-prompt")
            aid, bid = a.id, b.id
            _with_history(orch)
            for rid in (aid, bid):
                orch.record(rid).auto_archive_opt_out_reason = "manual-unarchive"
            client.portal.call(orch._persist_status, aid, "complete")
            assert _wait_until(lambda: _release_finished(orch))

            with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                ws.send_text(json.dumps({"type": "attach", "sessionId": aid}))
                _drain_until(ws, until_type="replayComplete")

                client.portal.call(_wedge_spawn, orch, aid, released)
                try:
                    # A's status event fires an auto re-attach that wedges inside
                    # ensure_driver holding A's attach lock.
                    client.portal.call(orch._persist_status, aid, "in_progress")
                    time.sleep(0.7)   # let it get INTO the resume before B's prompt

                    ws.send_text(json.dumps({"type": "prompt", "sessionId": bid,
                                             "text": "hello b"}))
                    delivered = _wait_until(
                        lambda: orch._last_prompt.get(bid) == "hello b", timeout=15.0)
                    # Order, not wall clock: B's prompt must land while A is STILL
                    # wedged. A loaded runner can make any budget look like a
                    # regression; "before the wedge let go" cannot.
                    assert delivered and not released, (
                        "a prompt on session B must not queue behind a wedged auto "
                        f"re-attach for session A on the same socket "
                        f"(delivered={delivered}, wedge released={bool(released)})")
                finally:
                    client.portal.call(_release_spawn, orch, aid, released)

            for rid in (aid, bid):
                client.portal.call(orch.close_session, rid)


def test_teardown_unsubscribes_even_with_an_auto_reattach_in_flight(tmp_path):
    """Teardown must unwind promptly with an auto re-attach still in flight.

    Gating the cleanup loop on a lock a wedged `_handle_attach` can hold for 90 s
    makes the whole `finally` an unbounded await: a cancellation arriving during it
    (SIGTERM drain, app shutdown) skips the reaps AND `unsubscribe_events`, orphaning
    the forwarding tasks and leaving a queue the orchestrator publishes into for the
    life of the process (#1164's rule about teardown paths)."""
    with _Harness(tmp_path) as h:
        orch = h.orch
        unsubscribed: list[float] = []
        released: list[float] = []
        real_unsubscribe = orch.unsubscribe_events

        def _counting_unsubscribe(q):
            # Timestamped at the call, so the assertion compares two SERVER-side
            # instants and not the test thread's polling latency.
            unsubscribed.append(time.monotonic())
            return real_unsubscribe(q)

        with TestClient(h.app, raise_server_exceptions=False) as client:
            session = client.portal.call(orch.create_session, "auto-reattach-teardown")
            sid = session.id
            _with_history(orch)
            orch.record(sid).auto_archive_opt_out_reason = "manual-unarchive"
            client.portal.call(orch._persist_status, sid, "complete")
            assert _wait_until(lambda: _release_finished(orch))

            with patch.object(orch, "unsubscribe_events", _counting_unsubscribe):
                with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                    ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
                    _drain_until(ws, until_type="replayComplete")

                    # Held long enough that a teardown which DOES wait on the
                    # re-attach's lock is unmistakable — and bounded, so a regression
                    # fails in ~25 s instead of on the CI shard timeout.
                    client.portal.call(_wedge_spawn, orch, sid, released, 25.0)
                    client.portal.call(orch._persist_status, sid, "in_progress")
                    time.sleep(0.7)   # the auto re-attach is now inside the resume
                # ...and the socket closes on top of it.
                assert _wait_until(lambda: bool(unsubscribed), timeout=40.0), (
                    "teardown must unsubscribe the lifecycle queue even with an "
                    "auto re-attach in flight")
                # Order, not wall clock: the unsubscribe must have happened while the
                # re-attach was STILL wedged. A teardown that waits on it can only
                # unsubscribe after the wedge lets go.
                assert not released or unsubscribed[0] < released[0], (
                    "teardown waited for the wedged auto re-attach instead of "
                    "reaping it")

            client.portal.call(_release_spawn, orch, sid, released)
            client.portal.call(orch.close_session, sid)
