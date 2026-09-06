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
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

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
    detach removes the session from the socket's attached-set. Observed via a
    terminator attach: the only replayReset in the drain is the terminator's own."""
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

                # Terminator: frames are handled in order, so this attach bounds the
                # drain (a regression that emits nothing must FAIL, not hang).
                ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
                events = _drain_until(ws, until_type="replayComplete", timeout=8.0)
                resets = [e for e in events if e.get("type") == "replayReset"]
                assert len(resets) == 1, (
                    f"detached socket must not auto re-attach; saw {len(resets)} "
                    "replayReset frames (expected only the terminator's)")

            client.portal.call(orch.close_session, sid)
