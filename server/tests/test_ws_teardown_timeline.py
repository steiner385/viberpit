"""The socket-teardown phase timeline (#1383).

`test_ws_auto_reattach.py::test_teardown_unsubscribes_even_with_an_auto_reattach_in_flight`
has failed on `server-tests (2)` a dozen times with `unsubscribe_events` uncalled 240s
after the socket closed — more than twice the ceiling every bound in that path adds up
to. Every theory advanced for it (real SDK subprocesses, executor starvation, broad
runner load, a stale bound method) has since been falsified, so the next occurrence
needs to arrive carrying evidence rather than another round of inference.

These tests pin the reporting contract that evidence depends on. They are pure — no
socket, no orchestrator — because the emitter is the only part that can be checked
without reproducing a flake nobody can reproduce on demand.
"""

import logging

from server.bridge.ws import _TEARDOWN_BUDGET_S, _emit_teardown_timeline

_LOGGER = "server.bridge.ws"


def test_over_budget_teardown_warns_with_the_phase_deltas(caplog):
    """An over-budget teardown must be loud, and must name the phase that ate it.

    WARNING specifically: pytest surfaces captured WARNING records on failure without
    any per-run log configuration, and the shard log is the only artifact available
    after a CI-only flake. A DEBUG line here would be correct and invisible.
    """
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    # One phase blows the whole budget on its own; the rest are instant.
    stall = _TEARDOWN_BUDGET_S + 30.0
    _emit_teardown_timeline([
        ("enter", 0.0),
        ("drain_work_chains", 0.10),
        ("reap_work", 0.15),
        ("reap_events", stall),
        ("reap_auto_reattach", stall + 0.01),
        ("detach_live", stall + 0.02),
        ("reap_batcher", stall + 0.03),
        ("unsubscribe_events", stall + 0.04),
    ])

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "an over-budget teardown must warn exactly once"
    msg = warnings[0].getMessage()
    assert "OVER BUDGET" in msg
    # The stalling phase is reported as its OWN cost, not as a cumulative offset —
    # every other phase must still read as instant so the culprit is unambiguous.
    assert f"reap_events={stall - 0.15:.2f}s" in msg, msg
    assert "reap_auto_reattach=0.01s" in msg, msg
    assert "unsubscribe_events=0.01s" in msg, msg


def test_a_teardown_inside_its_budget_stays_at_debug(caplog):
    """The everyday case is one line per closed socket — it must not reach the journal
    at WARNING, or a busy edge would drown in them."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    _emit_teardown_timeline([
        ("enter", 0.0),
        ("drain_work_chains", 0.02),
        ("reap_work", 0.03),
        ("reap_events", 0.04),
        ("reap_auto_reattach", 0.05),
        ("detach_live", 0.06),
        ("reap_batcher", 0.07),
        ("unsubscribe_events", 0.08),
    ])

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(debug) == 1
    assert "ws.teardown" in debug[0].getMessage()


def test_no_marks_emits_nothing(caplog):
    """Belt and braces: the emitter runs in a `finally`, so it must tolerate being
    reached before a single mark was recorded rather than raising there and masking
    whatever was already unwinding."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    _emit_teardown_timeline([])
    assert not caplog.records
