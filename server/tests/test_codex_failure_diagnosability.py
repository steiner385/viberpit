"""One Codex session's failure cascade, and the five auto-error issues it filed.

Incident (2026-09-09 → 2026-09-16, dobby): session ``37e13a96`` lost its Codex
app-server. Every stage of the fallout reported itself in a form that named
neither the cause nor the session's actual problem, and the error sink filed a
separate issue for each stage:

* ``codex.driver.resync_failed error=runtime_error`` (#1861) — every
  ``CodexProtocolError`` subclasses ``RuntimeError``, so ``scrub_exception``
  collapsed a closed transport, a timeout and a peer rejection onto one
  indistinguishable category. The record named the failure it could not describe.
* ``reassign.error`` with a traceback through ``_run_owned_operation`` (#1859) —
  the rebuild could not reach an app-server that had already closed. Retryable
  infrastructure state, logged as a fault.
* ``session.transcript_events … replay failed`` ×66 (#1852) and ×4 (#1860) — the
  titling and summary sweeps re-read a thread the app-server would not load, once
  per tick, each with a full traceback. Two issues, one cause, neither carrying it.
* ``recover.paused reason=all_accounts_busy`` ×2455 (#1702) — the stuck-session
  sweep re-parks an already-parked session every tick, and pausing is idempotent
  by design, so the 2,455th record said exactly what the first did.

The common shape — the #1767 / #1778 pattern a third and fourth time — is an
*expected, handled outcome* reported as a fault, in a loop. The fixes are
symmetrical: say which failure it was, drop the traceback that adds nothing, and
pace the record so one stuck session cannot dominate the journal. Behaviour
(pause reasons, retryability, the published events) is deliberately unchanged.

Verified against the real binary (Principle III), codex-cli 0.153.2: an unknown
method, a not-initialized call, bad params AND an unloaded thread all answer
``-32600``. The issue text's ``-32601`` is not a code this build emits, and no
single code distinguishes a transient fault from a permanently unreadable thread
— which is why the fix is diagnosability and pacing, not a capability latch.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

import server.sessions.orchestrator as orch_mod
from server.codex import protocol as codex_protocol
from server.codex import redaction
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


# ---------------------------------------------------------------------------
# #1861 — a transport failure must name itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (codex_protocol.CodexTransportClosed("closed"), "transport_closed"),
        (codex_protocol.CodexRequestTimeout("slow"), "timeout"),
        (codex_protocol.CodexFramingError("junk"), "framing_error"),
        (codex_protocol.CodexNotInitializedError("early"), "not_initialized"),
        (codex_protocol.CodexRequestOverloaded("full"), "overloaded"),
        (codex_protocol.CodexExternalAuthDisabled("off"), "external_auth_disabled"),
        (codex_protocol.CodexProtocolError("other"), "protocol_error"),
    ],
)
def test_each_app_server_failure_reduces_to_its_own_category(exc, category):
    """The #1861 fix. Before it every one of these read ``runtime_error``."""
    assert redaction.scrub_exception(exc) == category


def test_a_peer_rejection_carries_its_protocol_code():
    """The numeric code is the one peer-supplied value this module has always
    relayed (see its docstring). ``from_wire`` only accepts a real ``int``."""
    exc = codex_protocol.CodexRequestFailed(-32600, method="thread/read")
    assert redaction.scrub_exception(exc) == "peer_error:-32600"


def test_every_protocol_category_is_distinguishable():
    """The defect in #1861 was collision, so assert the absence of collision
    rather than just the presence of the names."""
    excs = [
        codex_protocol.CodexTransportClosed("x"),
        codex_protocol.CodexRequestTimeout("x"),
        codex_protocol.CodexFramingError("x"),
        codex_protocol.CodexNotInitializedError("x"),
        codex_protocol.CodexRequestOverloaded("x"),
        codex_protocol.CodexExternalAuthDisabled("x"),
        codex_protocol.CodexRequestFailed(-32600),
        codex_protocol.CodexProtocolError("x"),
    ]
    categories = [redaction.scrub_exception(e) for e in excs]
    assert len(set(categories)) == len(categories), f"categories collide: {categories}"


def test_a_peer_message_still_never_survives_the_scrubber():
    """The taxonomy widened what is reported; it must not widen what is relayed."""
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345"
    exc = codex_protocol.CodexRequestFailed.from_wire(
        {"code": -32600, "message": f"upstream rejected {secret}"}, method="thread/read"
    )
    rendered = redaction.scrub_exception(exc)
    assert secret not in rendered
    assert "upstream" not in rendered
    assert rendered == "peer_error:-32600"


def test_a_peer_supplied_non_int_code_cannot_ride_out_through_the_category():
    """`from_wire` is the only constructor the wire reaches, and it drops any
    code that is not exactly an ``int`` — so no peer string reaches the log."""
    exc = codex_protocol.CodexRequestFailed.from_wire(
        {"code": "'; DROP TABLE sessions; --"}, method="thread/read"
    )
    assert redaction.scrub_exception(exc) == "peer_error:0"


def test_non_codex_exceptions_keep_their_existing_categories():
    """Guard against the new branch shadowing the general taxonomy."""
    assert redaction.scrub_exception(RuntimeError("native")) == "runtime_error"
    assert redaction.scrub_exception(TimeoutError("native")) == "timeout"
    assert redaction.scrub_exception(OSError("native")) == "transport_error"
    assert redaction.scrub_exception(ValueError("native")) == "validation_error"
    assert redaction.scrub_exception(Exception("native")) == "internal_error"


# ---------------------------------------------------------------------------
# #1852 / #1860 — an unreadable thread is an answer, not a fault
# ---------------------------------------------------------------------------


class _FailingReplay:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def events(self, *, max_turns=None):
        raise self._exc

    async def window(self, **kw):
        raise self._exc


def _orch_with_failing_replay(monkeypatch, exc):
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)

    async def replay(session_id):
        return _FailingReplay(exc)

    monkeypatch.setattr(SessionOrchestrator, "_engine_replay",
                        lambda self, sid: replay(sid))
    orch_mod._REPLAY_FAILURE_LOG.clear()
    return o


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["transcript_events", "transcript_window",
                                     "engine_replay_window"])
async def test_an_app_server_refusal_is_logged_without_a_traceback(
        monkeypatch, caplog, surface):
    """Six stack frames from here to the transport say nothing the category does
    not. The traceback is what made #1852 and #1860 two issues instead of none."""
    o = _orch_with_failing_replay(
        monkeypatch, codex_protocol.CodexTransportClosed("client was closed"))

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        await getattr(o, surface)("37e13a96")

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.exc_info is None, "expected refusal logged with a traceback"
    assert "transport_closed" in record.getMessage(), record.getMessage()
    assert surface in record.getMessage()


@pytest.mark.asyncio
async def test_an_unexpected_failure_keeps_its_traceback(monkeypatch, caplog):
    """Guard against widening this into 'replay never reports a real bug'."""
    o = _orch_with_failing_replay(monkeypatch, AttributeError("genuine bug"))

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        assert await o.transcript_events("37e13a96") == []

    assert caplog.records[0].exc_info is not None
    assert "AttributeError" in caplog.records[0].getMessage()


@pytest.mark.asyncio
async def test_a_sweep_re_reading_a_dead_thread_reports_once_not_once_per_tick(
        monkeypatch, caplog):
    """#1852 was 66 occurrences of one unreadable thread. The titling and summary
    sweeps re-enter every tick for as long as the session exists."""
    o = _orch_with_failing_replay(
        monkeypatch, codex_protocol.CodexTransportClosed("client was closed"))

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        for _ in range(66):
            await o.transcript_events("37e13a96")

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, f"{len(warnings)} records for one stuck thread"


@pytest.mark.asyncio
async def test_replay_still_returns_the_empty_answer_its_callers_expect(monkeypatch):
    """Behaviour is unchanged: only the reporting differs. A caller that treats
    []/None as 'no readable history' must keep seeing exactly that."""
    exc = codex_protocol.CodexTransportClosed("client was closed")
    o = _orch_with_failing_replay(monkeypatch, exc)

    assert await o.transcript_events("37e13a96") == []
    assert await o.transcript_window("37e13a96") is None
    assert await o.engine_replay_window("37e13a96") is None


@pytest.mark.asyncio
async def test_a_different_session_is_not_silenced_by_another_sessions_window(
        monkeypatch, caplog):
    o = _orch_with_failing_replay(
        monkeypatch, codex_protocol.CodexTransportClosed("client was closed"))

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        await o.transcript_events("session-a")
        await o.transcript_events("session-b")

    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 2


# ---------------------------------------------------------------------------
# #1702 — pausing is idempotent, so its record must be paced
# ---------------------------------------------------------------------------


def _pause_orch(monkeypatch):
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o.events = []
    o._publish_event = o.events.append

    async def persist(session_id, status):
        return None

    o._persist_status = persist
    orch_mod._PAUSE_LOG.clear()
    return o


def _paused_rec(tmp_path):
    return SessionRecord(id="37e13a96", name="n", account="a",
                         engine=AgentEngine.CODEX, engine_session_id="t1",
                         cols=80, rows=24, cwd=str(tmp_path))


@pytest.mark.asyncio
async def test_re_pausing_an_already_paused_session_reports_once(
        monkeypatch, caplog, tmp_path):
    """2455 identical records for one session (#1702): the stuck-session sweep
    re-enters recovery every tick and lands on this exact line each time."""
    o = _pause_orch(monkeypatch)
    rec = _paused_rec(tmp_path)

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        for _ in range(2455):
            await o._pause_session(rec, reason="all_accounts_busy")

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, f"{len(warnings)} records for one parked session"
    assert "reason=all_accounts_busy" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_the_state_write_and_the_paused_event_are_not_throttled(
        monkeypatch, tmp_path):
    """Only the journal line is paced. The event is what the dashboard reads, and
    a client attaching after the first pause still has to be told why."""
    o = _pause_orch(monkeypatch)
    rec = _paused_rec(tmp_path)

    for _ in range(5):
        await o._pause_session(rec, reason="all_accounts_busy")

    assert len(o.events) == 5
    assert all(e["type"] == "paused" for e in o.events)
    assert rec.paused_reason == "all_accounts_busy"


@pytest.mark.asyncio
async def test_a_shifting_reset_time_does_not_re_open_the_window(
        monkeypatch, caplog, tmp_path):
    """`earliest_reset` rides the same call but is not in the record. Treating a
    moved reset time as news would re-report on any tick that shifted it — which
    is exactly the 2455-record defect, reintroduced through the freshness check."""
    from datetime import datetime, timedelta

    o = _pause_orch(monkeypatch)
    rec = _paused_rec(tmp_path)
    base = datetime(2026, 9, 16, 10, 0, 0)

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        for tick in range(50):
            await o._pause_session(rec, reason="all_accounts_busy",
                                   earliest_reset=base + timedelta(seconds=tick))

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, f"{len(warnings)} records for a drifting reset time"


@pytest.mark.asyncio
async def test_a_change_of_reason_always_reports_however_recent_the_last_one(
        monkeypatch, caplog, tmp_path):
    """The transition is the record an operator is watching for; throttling it
    would hide the one line carrying new information behind the old one."""
    o = _pause_orch(monkeypatch)
    rec = _paused_rec(tmp_path)

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        await o._pause_session(rec, reason="all_accounts_busy")
        await o._pause_session(rec, reason="all_accounts_busy")
        await o._pause_session(rec, reason="crash_cap")

    reasons = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(reasons) == 2, reasons
    assert "reason=crash_cap" in reasons[1]


# ---------------------------------------------------------------------------
# #1859 — a rebuild that cannot reach its engine is retryable, not a fault
# ---------------------------------------------------------------------------


def _reassign_orch():
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 1000.0
    o.paused = []

    async def spy_pause(record, reason, earliest_reset=None):
        o.paused.append((record.id, reason))

    o._pause_session = spy_pause
    o._copilot = None
    o._suggester = None
    o._session_factory = lambda sid: lambda *a, **k: None
    o._install_driver = lambda sid, drv: o._drivers.__setitem__(sid, drv)
    # The capability Codex actually declares, and the one the incident took: it
    # cannot carry a thread across accounts, so a rotation starts a fresh one —
    # `driver.start()` → `start_thread(...)`, which is where the traceback in
    # #1859 begins. A test registry has no Codex factory (it is env-gated), so
    # without this the reassign is refused before it can reach the classifier.
    o._engine_supports = lambda engine, cap: cap == "account_switch_new_thread"
    return o


async def _run_reassign_with_start_failure(o, monkeypatch, tmp_path, exc):
    rec = SessionRecord(id="37e13a96", name="n", account="a",
                        engine=AgentEngine.CODEX, engine_session_id="t1",
                        cols=80, rows=24, cwd=str(tmp_path))
    o._records[rec.id] = rec

    class _OldDriver:
        engine_session_id = "t1"
        cwd = str(tmp_path)
        mode = "interactive"
        identity = "user@example.com"

        async def interrupt(self): pass
        async def aclose(self): pass

    o._drivers[rec.id] = _OldDriver()

    class _NewDriver:
        engine_session_id = None
        mode = "interactive"

        async def start(self): raise exc
        async def interrupt(self): pass
        async def aclose(self): pass
        async def set_model(self, m): pass

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_create_driver",
                        lambda self, **kw: _NewDriver())

    async def allow(session_id, *, why, adopt=False):
        return True

    o._claim_session = allow
    o._publish_event = lambda e: None

    class _MockDB:
        async def execute(self, *a): pass

    class _FakeDbCtx:
        async def __aenter__(self): return _MockDB()
        async def __aexit__(self, *a): pass

    monkeypatch.setattr(orch_mod, "db_session", lambda: _FakeDbCtx())
    monkeypatch.setattr(orch_mod, "compose", lambda acct: Path("/fake/cfg"))
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)
    return await o._reassign(rec, "b", reason="process_crash")


@pytest.mark.asyncio
async def test_an_unreachable_app_server_is_not_logged_as_an_error(
        monkeypatch, caplog, tmp_path):
    """The #1767 / #1778 shape a third time: an ERROR-with-traceback for
    infrastructure state files an auto-error issue nobody can act on."""
    o = _reassign_orch()

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        outcome = await _run_reassign_with_start_failure(
            o, monkeypatch, tmp_path,
            codex_protocol.CodexTransportClosed("client was closed"))

    assert outcome == "failed"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], f"engine outage logged as ERROR: {[r.message for r in errors]}"
    message = caplog.records[0].getMessage()
    assert "reassign.engine_unavailable" in message
    assert "transport_closed" in message, "the cause the ERROR form never carried"


@pytest.mark.asyncio
async def test_an_unreachable_app_server_stays_retryable(monkeypatch, tmp_path):
    """A closed app-server comes back when the runner is respawned, so the sweep
    must keep retrying — this must NOT become a terminal pause reason."""
    o = _reassign_orch()
    await _run_reassign_with_start_failure(
        o, monkeypatch, tmp_path,
        codex_protocol.CodexTransportClosed("client was closed"))

    assert o.paused == [("37e13a96", "reassign_failed:process_crash")]
    assert "reassign_failed:process_crash" not in orch_mod._TERMINAL_PAUSE_REASONS


@pytest.mark.asyncio
async def test_a_non_protocol_start_failure_still_logs_an_error(
        monkeypatch, caplog, tmp_path):
    """Guard against widening this into 'a failed reassign is never an error'."""
    o = _reassign_orch()

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        await _run_reassign_with_start_failure(
            o, monkeypatch, tmp_path, RuntimeError("CLI exited 1"))

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "reassign.error" in errors[0].getMessage()
