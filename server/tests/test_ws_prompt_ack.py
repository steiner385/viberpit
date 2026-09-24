"""The edge must ACK a prompt's receipt before it starts working on it.

A prompt frame used to produce nothing observable until the driver echoed the
user message back — which happens after ``_ensure_live`` has (possibly) woken a
detached session and after the SDK has started the turn. On a resumed session
that is 30 s–2 min of a completely silent socket, so the browser could not tell
a received prompt from one that fell on the floor.

There is already a NEGATIVE ack (``promptRejected``). These tests pin the
positive one:

  * ``promptAck stage=received|resuming`` — emitted BEFORE the slow wake-up, so
    it is the FIRST thing the browser hears back. Ordering is the whole point:
    an ack sent after ``_ensure_live`` would answer the question 90 seconds
    after it was asked.
  * ``promptAck stage=delivered`` — the driver has the text; a turn starts next.
"""
from __future__ import annotations

import json
import logging

from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events
from server.tests.test_ws import ws_env as ws_env  # noqa: F401 — fixture lives in test_ws


def _attach(ws, session_id):
    ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
    _drain_events(ws, until_type="replayComplete")


def _acks(frames):
    return [f for f in frames if f.get("type") == "promptAck"]


def test_prompt_is_acked_before_the_turn_produces_anything(ws_env):
    """The ack must be the FIRST frame back — ahead of the user echo."""
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "ack-me")
    sid = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        _attach(ws, sid)
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "hello"}))
        frames = _drain_events(ws, until=lambda f: f.get("type") == "user")

    acks = _acks(frames)
    assert acks, f"no promptAck frame was ever sent; got {[f.get('type') for f in frames]!r}"

    first_ack = next(i for i, f in enumerate(frames) if f.get("type") == "promptAck")
    echoes = [i for i, f in enumerate(frames) if f.get("type") == "user"]
    assert echoes, "test did not observe the user echo it drains to"
    assert first_ack < echoes[0], (
        "promptAck arrived AFTER the turn's user echo — an ack that waits for the "
        "work it is acknowledging tells the user nothing they did not already see"
    )


def test_receipt_ack_names_the_session_and_a_receipt_stage(ws_env):
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "ack-shape")
    sid = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        _attach(ws, sid)
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "hello"}))
        frames = _drain_events(ws, until=lambda f: f.get("type") == "user")

    receipt = _acks(frames)[0]
    assert receipt["sessionId"] == sid
    assert receipt["stage"] in ("received", "resuming"), receipt


def test_delivered_ack_follows_the_receipt_ack(ws_env):
    """Two acks, in order: the edge has it, then the driver has it."""
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "two-acks")
    sid = session.id

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        _attach(ws, sid)
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "hello"}))
        frames = _drain_events(
            ws, until=lambda f: f.get("type") == "promptAck" and f.get("stage") == "delivered")

    stages = [f["stage"] for f in _acks(frames)]
    assert stages[0] in ("received", "resuming"), stages
    assert "delivered" in stages, stages
    assert stages.index("delivered") > 0, stages


def test_a_refused_prompt_is_not_acked_as_delivered(ws_env):
    """A drain rejects the prompt. The receipt ack is still honest (we DID receive
    it), but nothing may claim it reached the driver."""
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "drained")
    sid = session.id

    async def refuse(session_id, text, attachment_ids=None, **kw):
        return "draining"

    orch.send_prompt = refuse  # type: ignore[method-assign]

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        _attach(ws, sid)
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "hello"}))
        frames = _drain_events(ws, until=lambda f: f.get("type") == "promptRejected")

    stages = [f["stage"] for f in _acks(frames)]
    assert "delivered" not in stages, (
        f"claimed delivery of a prompt the server refused; acks={stages!r}")
    assert any(f.get("type") == "promptRejected" for f in frames)


def test_a_failed_wake_acks_the_prompt_as_failed(ws_env):
    """The wake itself can fail (SESSION_NOT_FOUND / SESSION_START_FAILED /
    SESSION_RESUME_TIMEOUT). The receipt ack already promised "resuming", and
    until now nothing ever contradicted it: the error frame is a banner, not a
    stage, so the bubble under the prompt spun on "waking session…" forever
    (2026-09-14, the assistant drawer). The terminal stage must ride the SAME
    promptAck channel as every other stage — one vocabulary, no error-code list
    for the browser to keep in sync with the edge."""
    _app, orch, client = ws_env([])

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        # Never attached, unknown to the orchestrator: _ensure_live's resume path
        # runs and ensure_driver answers None.
        ws.send_text(json.dumps({"type": "prompt", "sessionId": "ghost", "text": "hello"}))
        # Two-phase drain, so a MISSING ack fails instead of hanging: receive_text()
        # blocks and the drain's timeout only ticks between frames. Phase 1 stops at
        # the replayComplete every attach failure pays. The failed ack is sent by the
        # same chain task immediately after, so by the time this client has read
        # replayComplete, round-tripped a sentinel, and the receive loop has answered
        # it, that ack is long since on the wire — phase 2 stops at the sentinel's
        # INVALID_JSON reply, which the failed ack precedes.
        frames = _drain_events(ws, until_type="replayComplete")
        ws.send_text("not json")
        frames += _drain_events(
            ws, until=lambda f: f.get("type") == "error" and f.get("code") == "INVALID_JSON")

    stages = [f["stage"] for f in _acks(frames)]
    assert stages and stages[0] == "resuming", stages
    assert stages[-1] == "failed", (
        f"a wake that failed left the prompt in flight; acks={stages!r}")
    assert "delivered" not in stages, stages
    codes = [f.get("code") for f in frames if f.get("type") == "error"]
    assert "SESSION_NOT_FOUND" in codes, codes
    # The error explains WHY; the failed ack must not overtake it.
    err_idx = next(i for i, f in enumerate(frames) if f.get("type") == "error")
    failed_idx = next(i for i, f in enumerate(frames)
                      if f.get("type") == "promptAck" and f.get("stage") == "failed")
    assert err_idx < failed_idx, [f.get("type") for f in frames]


class _NoRecheckSeam:
    """Stands in for `app.state.sessions` in production shape: edge -> FleetOrchestrator
    -> RemoteOrchestrator. `recheck_stuck_complete_session` is a daemon-local,
    in-memory-record read with no RPC wiring, so RemoteOrchestrator has no such
    method AND (unlike `InProcessOrchestratorAdapter`, which forwards it through its
    OWN defensive `getattr(self._orch, name, None)`) no generic `__getattr__`
    fallback either — plain attribute lookup on it raises AttributeError. Wrapping
    the real (dev-path) orchestrator and blocking just this one name reproduces that
    gap without hand-rolling the rest of FleetOrchestrator's surface."""
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name == "recheck_stuck_complete_session":
            raise AttributeError(name)
        return getattr(self._inner, name)


def test_a_failed_wake_from_stale_ownership_still_acks_failed_without_the_recheck_seam(
        ws_env):  # noqa: F811
    """StaleOwnership's fire-and-forget ``recheck_stuck_complete_session`` call used
    to be reached as a bare, unguarded attribute access. In production ``orchestrator``
    is a FleetOrchestrator whose primary is a RemoteOrchestrator with no such method
    and no generic ``__getattr__`` fallback (unlike ``InProcessOrchestratorAdapter``,
    the dev/CI path, which already defends this one internally — see
    ``server/sessions/facade.py`` — which is exactly why this bug shipped invisibly:
    every existing test in this file runs through the adapter and can't see it). The
    resulting AttributeError fired from INSIDE the except block that was supposed to
    send the SESSION_OWNERSHIP_STALE banner and let ``_handle_prompt`` ack the prompt
    as ``failed``; it escaped both, got swallowed by ``_work_chain``'s catch-all, and
    the browser's optimistic "resuming" pill never got corrected — it spun on
    "Sent — waking…" forever (dobby, 2026-09-24, three sessions whose PRs had already
    merged)."""
    from server.automations.ownership import StaleOwnership

    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "stale-ownership-no-seam")
    sid = session.id
    _app.state.sessions = _NoRecheckSeam(_app.state.sessions)

    async def _boom(_sid, *_a, **_k):
        raise StaleOwnership("operation no longer owns its targets")
    orch.ensure_driver = _boom  # type: ignore[method-assign]

    with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
        ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "hello"}))
        frames = _drain_events(
            ws, until=lambda f: f.get("type") == "promptAck" and f.get("stage") == "failed")

    stages = [f["stage"] for f in _acks(frames)]
    assert stages and stages[0] == "resuming", stages
    assert stages[-1] == "failed", (
        f"a StaleOwnership wake failure with no recheck seam left the prompt "
        f"stuck in flight instead of acking it failed; acks={stages!r}")
    codes = [f.get("code") for f in frames if f.get("type") == "error"]
    assert "SESSION_OWNERSHIP_STALE" in codes, codes


def test_a_failed_wake_does_not_log_a_second_warning(ws_env, caplog):  # noqa: F811
    """A failed wake already gets a WARNING with the real reason from
    ``_resume_driver_or_error`` (e.g. SESSION_NOT_FOUND / SESSION_START_FAILED),
    which is what the client-facing error frame explains. The ``_handle_prompt``
    call site used to log a SECOND, less specific WARNING
    ("ws.prompt: wake failed, prompt not delivered") for the exact same failure —
    two auto-filed error-triage issues (#1783, #1784) for one incident. That
    consequence-only log carries no diagnostic info the upstream WARNING lacks,
    so it must not itself sit at WARNING (issue #1783)."""
    _app, orch, client = ws_env([])

    with caplog.at_level(logging.INFO, logger="server.bridge.ws"):
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "prompt", "sessionId": "ghost2", "text": "hi"}))
            _drain_events(ws, until_type="replayComplete")
            ws.send_text("not json")
            _drain_events(
                ws, until=lambda f: f.get("type") == "error" and f.get("code") == "INVALID_JSON")

    wake_failed = [r for r in caplog.records if "wake failed" in r.message]
    assert wake_failed, "expected the wake-failed consequence log to still fire"
    assert all(r.levelno < logging.WARNING for r in wake_failed), (
        f"wake-failed log must not be WARNING+ (duplicates the upstream reason "
        f"log and double-files auto-error issues): {[r.levelno for r in wake_failed]!r}"
    )


def _resume_events(caplog):
    return [r for r in caplog.records if getattr(r, "event", None) == "ws.session.resume"]


def test_a_failed_resume_measurement_is_not_itself_a_warning(ws_env, caplog):  # noqa: F811
    """``ws.session.resume ok=false`` is the MEASUREMENT of a lazy resume (#1180),
    emitted by ``_ensure_live`` after ``_resume_driver_or_error`` already reported the
    failure to the client. Error triage files every WARNING+ record as its own
    auto-error issue, so the measurement sitting at WARNING re-filed each incident
    that ``_resume_driver_or_error`` had already logged (#1784, twin of #1783). It
    must still fire — the resume-cost numbers need the failures too — just below
    WARNING."""
    _app, orch, client = ws_env([])

    with caplog.at_level(logging.INFO, logger="server.bridge.ws"):
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "prompt", "sessionId": "ghost3", "text": "hi"}))
            _drain_events(ws, until_type="replayComplete")
            ws.send_text("not json")
            _drain_events(
                ws, until=lambda f: f.get("type") == "error" and f.get("code") == "INVALID_JSON")

    resumes = _resume_events(caplog)
    assert resumes, "the failed resume must still be measured"
    assert all(r.fields["ok"] is False for r in resumes), resumes  # type: ignore[attr-defined]
    assert all(r.levelno < logging.WARNING for r in resumes), (
        f"ws.session.resume ok=false must not be WARNING+ (re-files the incident "
        f"_resume_driver_or_error already reported): {[r.levelno for r in resumes]!r}")


def test_a_resume_that_raises_files_exactly_one_warning(ws_env, caplog):  # noqa: F811
    """An ``ensure_driver`` that raises is logged at WARNING once, with its traceback,
    by ``_resume_driver_or_error`` — the measurement must not add a second."""
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "resume-raises")
    sid = session.id

    async def _boom(_sid, *_a, **_k):
        raise RuntimeError("token dead")
    orch.ensure_driver = _boom  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO, logger="server.bridge.ws"):
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "prompt", "sessionId": sid, "text": "hi"}))
            _drain_events(ws, until_type="replayComplete")
            ws.send_text("not json")
            _drain_events(
                ws, until=lambda f: f.get("type") == "error" and f.get("code") == "INVALID_JSON")

    warnings = [r for r in caplog.records
                if r.name == "server.bridge.ws" and r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [(r.levelno, r.getMessage()) for r in warnings]
    assert "ensure_driver failed" in warnings[0].getMessage()
    assert _resume_events(caplog), "the failed resume must still be measured"
