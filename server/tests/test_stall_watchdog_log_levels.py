"""The stall watchdog's log levels must match what a human can act on (#1718).

Interrupting a wedged turn is the watchdog's *first-line automatic recovery*, and it
usually works: of 17 sessions interrupted on rocky between 2026-09-14 and 2026-09-23,
11 were interrupted exactly once and settled. Logged at WARNING it was captured by the
error sink (server/obs/errsink.py is a WARNING+ handler) and filed as an unactionable
auto-error issue — 45 occurrences of "the watchdog did its job" (#1718). The rest of
this module logs recovery ACTIONS at INFO (`disabled_account_sweep: rotating`,
`rotation.restart_driverless`, `reassign.complete`) and reserves WARNING for a recovery
that failed, which is exactly the distinction the escalation rungs already draw.

So: the interrupt is INFO; the two escalation rungs (recompose, and the pause once
recompose is spent) stay WARNING. And every watchdog record is emitted inside
``bind_session`` so the sid reaches the record — `extra={"session_id": ...}` cannot
work here, because ContextFilter *overwrites* that attribute from the contextvar
(jsonlog.py), which is why #1718's issue body shows "Sessions | —" with the sid sitting
in plain sight in the message text.
"""
import logging
from types import SimpleNamespace

import pytest

from server.obs.jsonlog import ContextFilter
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.recovery import CRASH_CAP, STALL_CAP
from server.usage import process_quota

# The same ledger module object the sweep imports, reached through a server module that
# already holds it rather than the legacy package path (brand ratchet, #1374).
_ledger = process_quota.ledger

SID = "f1b85a1e-7d2f-42d7-aa87-27b53cec9915"


class _Driver:
    def __init__(self, account="acct"):
        self.account = account
        self.turn_active = True
        self.drain_alive = False
        self.in_api_backoff = False
        self.awaiting_user_input = False
        self.last_progress_at = 0.0
        self.interrupted = False

    async def interrupt(self, *, keep_queue=False):
        self.interrupted = True


def _orch(driver):
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 10_000.0
    o._drivers[SID] = driver
    o._records[SID] = SimpleNamespace(id=SID, account=driver.account, status="working")
    return o


@pytest.fixture
def watchdog_logs(caplog):
    """Records from the orchestrator logger, with correlation ids stamped on.

    ContextFilter is a *handler* filter in production (jsonlog.py attaches it to the
    stream/file handlers, and it mutates the record so the error sink downstream sees
    the ids too). caplog's handler has none, so the test installs it to observe the
    same stamping the error sink relies on.
    """
    caplog.handler.addFilter(ContextFilter())
    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        yield caplog


@pytest.mark.asyncio
async def test_a_recovered_wedge_is_info_not_warning(watchdog_logs):
    driver = _Driver()
    o = _orch(driver)

    await o._check_stalls(10_000.0)

    assert driver.interrupted
    assert [r.message for r in watchdog_logs.records if r.levelno >= logging.WARNING] == [], \
        "a first-line interrupt is recovery, not a fault — it must not reach the error sink"
    assert any("interrupting wedged turn" in r.getMessage() and r.levelno == logging.INFO
               for r in watchdog_logs.records), "the interrupt must still be logged, at INFO"


@pytest.mark.asyncio
async def test_the_interrupt_record_names_its_session(watchdog_logs):
    o = _orch(_Driver())

    await o._check_stalls(10_000.0)

    interrupt = next(r for r in watchdog_logs.records if "interrupting wedged turn" in r.getMessage())
    assert getattr(interrupt, "session_id", None) == SID, \
        "without bind_session the filed issue's Sessions row is '—' (#1718)"


@pytest.mark.asyncio
async def test_the_interrupt_says_how_long_the_turn_was_silent(watchdog_logs):
    """The one line a human reads when a wedge does turn out to matter. Elapsed vs the
    timeout in force distinguishes a throttled turn (900 s) from a plain hang (300 s),
    and the streak says whether earlier interrupts landed."""
    o = _orch(_Driver())

    await o._check_stalls(10_000.0)

    msg = next(r.getMessage() for r in watchdog_logs.records if "interrupting wedged turn" in r.getMessage())
    assert "silent" in msg and "10000s" in msg, msg
    assert "timeout=300s" in msg, msg
    assert "streak=1" in msg, msg


@pytest.mark.asyncio
async def test_a_repeated_wedge_escalates_at_warning(watchdog_logs, monkeypatch):
    """Recompose — the rung reached once interrupts are provably not landing — is the
    condition worth filing, and it stays WARNING."""
    driver = _Driver()
    o = _orch(driver)
    reassigned = []

    async def _reassign(record, account, *, reason):
        reassigned.append((record.id, account, reason))

    monkeypatch.setattr(o, "_reassign", _reassign)
    for _ in range(STALL_CAP):
        o._recovery.record_stall(SID, now=10_000.0)

    await o._check_stalls(10_000.0)

    assert reassigned == [(SID, "acct", "stall_recompose")]
    warnings = [r for r in watchdog_logs.records if r.levelno >= logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        f"stall watchdog: recomposing wedged turn session={SID}"]
    assert getattr(warnings[0], "session_id", None) == SID


@pytest.mark.asyncio
async def test_a_spent_recompose_budget_pauses_at_warning(watchdog_logs, monkeypatch):
    driver = _Driver()
    o = _orch(driver)
    paused = []

    async def _pause_session(record, *, reason):
        paused.append((record.id, reason))

    monkeypatch.setattr(o, "_pause_session", _pause_session)
    for _ in range(STALL_CAP):
        o._recovery.record_stall(SID, now=10_000.0)
    for _ in range(CRASH_CAP):
        o._recovery.record_crash(SID, now=10_000.0)

    await o._check_stalls(10_000.0)

    assert paused == [(SID, "stall_cap")]
    warnings = [r for r in watchdog_logs.records if r.levelno >= logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        f"stall watchdog: recompose cap reached, pausing session={SID}"]
    assert getattr(warnings[0], "session_id", None) == SID


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [OSError(5, "Input/output error"), FileNotFoundError(2, "gone")])
async def test_a_transient_ledger_read_error_in_the_disabled_sweep_is_not_an_error(
        watchdog_logs, monkeypatch, exc):
    """#1945: a one-off EIO on the shared ~/.claude-accounts mount escaped the sweep and
    the stall loop's catch-all filed it via ``log.exception``. The sweep re-runs every
    30 s and is idempotent, so the next tick is the recovery — INFO, not an issue."""
    def _boom():
        raise exc

    monkeypatch.setattr(_ledger, "locked_ledger", _boom)
    o = _orch(_Driver())

    await o._check_disabled_account_sessions()

    assert [r.message for r in watchdog_logs.records if r.levelno >= logging.WARNING] == []
    assert any("disabled_account_sweep" in r.getMessage() and r.levelno == logging.INFO
               for r in watchdog_logs.records)


@pytest.mark.asyncio
async def test_a_non_io_ledger_error_still_propagates(monkeypatch):
    def _boom():
        raise ValueError("ledger.json is not JSON")

    monkeypatch.setattr(_ledger, "locked_ledger", _boom)
    o = _orch(_Driver())

    with pytest.raises(ValueError):
        await o._check_disabled_account_sessions()
