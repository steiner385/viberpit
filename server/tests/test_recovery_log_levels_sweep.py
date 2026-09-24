"""Sibling sweep for #1718 / #2314: a recovery that worked is not a warning (#2275).

``server/obs/errsink.py`` is a WARNING+ handler, so the log level of a recovery
record IS the filing decision. Two sites reported a *successful* automatic
recovery at WARNING, minting an auto-error occurrence every time the system
healed itself:

* ``SessionOrchestrator._recover_undelivered_prompts`` (rehydrate re-staging a
  prompt a daemon restart stranded before it ever ran — filed as #1979/#1980/#2172).
  The docstring on that method calls the re-stage "safe" by construction: a NULL
  ``engine_session_id`` proves the turn never ran, so redelivering it duplicates
  nothing. The real failure rungs, ``initial_prompt_recover: no driver for %s``
  and ``initial_prompt_recover: delivery failed for %s`` in
  ``_deliver_initial_prompt``, stay WARNING — they are the conditions worth an
  issue.
* the bridge's ``ws.prompt`` handler retrying a send after a transient
  ``no_driver`` (filed as #1976). That line only announces the retry attempt,
  not its outcome — if the rebuild or the retried send still fails, the
  already-WARNING ``ws.prompt rejected`` rung fires a few lines later.

These tests pin both demotions and both surviving failure rungs so a future
regression can't silently flatten the ladder back to all-WARNING.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from server.tests.test_orphan_initial_prompt import _init, _seed_spawned, _settle
from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events
from server.tests.test_ws import ws_env as ws_env  # noqa: F401 — fixture lives in test_ws


def _attach(ws, session_id):
    ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
    _drain_events(ws, until_type="replayComplete")


# --------------------------------------------------------------------------- #
# rehydrate's initial-prompt re-send (#1979, #1980, #2172)
# --------------------------------------------------------------------------- #

def test_rehydrate_recovery_is_info_not_warning(tmp_path, caplog):
    async def run():
        from server.sessions.orchestrator import SessionOrchestrator
        await _init(f"sqlite+aiosqlite:///{tmp_path / 'h.db'}")
        await _seed_spawned("human-1", origin=None)

        orch = SessionOrchestrator()

        async def _send(sid, prompt, attachment_ids=None):
            return None

        async def _driver(sid):
            return object()

        orch.send_prompt = _send  # type: ignore[method-assign]
        orch.ensure_driver = _driver  # type: ignore[method-assign]

        with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
            await orch.rehydrate()
            await _settle()

        events = [r.getMessage() for r in caplog.records]
        recovered_recs = [r for r in caplog.records
                           if r.getMessage() == "session.initial_prompt_recovered"]
        summary_recs = [r for r in caplog.records
                         if r.getMessage().startswith("rehydrate: recovered")]

        assert recovered_recs, f"no session.initial_prompt_recovered record seen: {events}"
        assert recovered_recs[0].levelno == logging.INFO
        assert recovered_recs[0].fields["session_id"] == "human-1"
        assert recovered_recs[0].fields["reason"] == "undelivered_across_restart"

        assert summary_recs, f"no rehydrate summary record seen: {events}"
        assert summary_recs[0].levelno == logging.INFO

    asyncio.run(run())


def test_undelivered_prompt_with_no_driver_still_warns(tmp_path, caplog):
    """The failure rung this recovery falls back to must be unchanged."""
    async def run():
        from server.sessions.orchestrator import SessionOrchestrator
        await _init(f"sqlite+aiosqlite:///{tmp_path / 'h.db'}")
        await _seed_spawned("human-2", origin=None)

        orch = SessionOrchestrator()

        async def _no_driver(sid):
            return None

        orch.ensure_driver = _no_driver  # type: ignore[method-assign]

        with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
            await orch.rehydrate()
            await _settle()

        no_driver_recs = [r for r in caplog.records
                           if r.getMessage().startswith("initial_prompt_recover: no driver")]
        assert no_driver_recs, "expected the no-driver failure rung to still fire"
        assert no_driver_recs[0].levelno == logging.WARNING

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the bridge's no_driver retry announcement (#1976)
# --------------------------------------------------------------------------- #

def test_ws_no_driver_retry_announcement_is_info(ws_env, caplog):
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "race-victim-2")
    sid = session.id

    real_send = orch.send_prompt
    calls: list[str] = []

    async def flaky_send(session_id, text, attachment_ids=None):
        calls.append(text)
        if len(calls) == 1:
            return "no_driver"
        return await real_send(session_id, text, attachment_ids)

    orch.send_prompt = flaky_send  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO, logger="server.bridge.ws"):
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            _attach(ws, sid)
            ws.send_text(json.dumps({"type": "prompt", "sessionId": sid,
                                     "text": "please do not vanish"}))
            deadline = time.monotonic() + 8.0
            while len(calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)

    assert len(calls) == 2, "the retry itself must still happen"
    retry_recs = [r for r in caplog.records
                  if r.getMessage().startswith("ws.prompt: no_driver")]
    assert retry_recs, "expected the no_driver retry announcement to be logged"
    assert retry_recs[0].levelno == logging.INFO


def test_ws_prompt_rejected_after_failed_rebuild_still_warns(ws_env, caplog):
    """The terminal failure rung this retry falls back to must be unchanged."""
    _app, orch, client = ws_env([])
    session = client.portal.call(orch.create_session, "hopeless-2")
    sid = session.id

    with caplog.at_level(logging.INFO, logger="server.bridge.ws"):
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            _attach(ws, sid)
            client.portal.call(lambda: orch._drivers.pop(sid, None))

            async def broken_ensure(session_id, identity=None, **kw):
                raise RuntimeError("rebuild impossible (test)")
            orch.ensure_driver = broken_ensure  # type: ignore[method-assign]

            ws.send_text(json.dumps({"type": "prompt", "sessionId": sid,
                                     "text": "held please"}))
            _drain_events(ws, until_type="promptRejected")

    rejected_recs = [r for r in caplog.records
                      if r.getMessage().startswith("ws.prompt rejected")]
    assert rejected_recs, "expected the terminal rejection rung to still fire"
    assert rejected_recs[0].levelno == logging.WARNING
