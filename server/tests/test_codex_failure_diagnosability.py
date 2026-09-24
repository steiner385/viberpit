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
* ``codex.manager.runner_crashed exit_code=<n>`` ×42 (#1857) — one event name for
  three unrelated endings, and the number that told them apart was normalized
  away. See that section below for the full shape.

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

import asyncio
import logging
from functools import partial
from pathlib import Path

import pytest

import server.sessions.orchestrator as orch_mod
from server.codex import manager as mgr_mod
from server.codex import protocol as codex_protocol
from server.codex import redaction, schema
from server.codex.capabilities import CodexAvailability
from server.obs.context import log_event
from server.obs.error_signature import signature_of
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_codex_app_server import FakeCodexAppServer


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
        (codex_protocol.CodexRequestUnrouted("nobody"), "unrouted"),
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
        codex_protocol.CodexRequestUnrouted("x"),
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
    assert "transport_closed" in record.fields["error"], record.fields
    assert record.fields["surface"] == surface


@pytest.mark.asyncio
async def test_an_unexpected_failure_keeps_its_traceback(monkeypatch, caplog):
    """Guard against widening this into 'replay never reports a real bug'."""
    o = _orch_with_failing_replay(monkeypatch, AttributeError("genuine bug"))

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        assert await o.transcript_events("37e13a96") == []

    assert caplog.records[0].exc_info is not None
    assert caplog.records[0].fields["error"] == "AttributeError"


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
# #1530 — a peer-rejected thread/read, not just a closed transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["transcript_events", "transcript_window",
                                     "engine_replay_window"])
async def test_a_peer_rejected_thread_read_is_logged_without_a_traceback(
        monkeypatch, caplog, surface):
    """#1530: the app-server's own JSON-RPC refusal of ``thread/read``
    (``-32600``, "thread not loaded") reaches these same three replay
    surfaces via ``CodexRequestFailed`` rather than the ``CodexTransportClosed``
    the surface-parametrized test above exercises. Both are ``CodexProtocolError``
    subclasses so ``_log_replay_failure`` classifies them identically, but #1530's
    own traceback names this exception specifically — pinned separately so a
    future change narrowed to ``CodexTransportClosed`` cannot silently regress it."""
    o = _orch_with_failing_replay(
        monkeypatch, codex_protocol.CodexRequestFailed(-32600, method="thread/read"))

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        result = await getattr(o, surface)("37e13a96")

    assert result in ([], None)
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.exc_info is None, "peer rejection logged with a traceback"
    assert "peer_error:-32600" in record.fields["error"], record.fields
    assert record.fields["surface"] == surface


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
    assert warnings[0].fields["reason"] == "all_accounts_busy"


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
async def test_a_newly_seen_reason_reports_however_recent_the_last_one(
        monkeypatch, caplog, tmp_path):
    """The transition is the record an operator is watching for; throttling it
    would hide the one line carrying new information behind the old one.

    It reports because a different reason is a different KEY, not because the
    window is re-armed on change. Re-arming was the same statement — and it is
    what let two retry loops alternating reasons flood one fingerprint (#1780,
    pinned in test_pause_reason_is_one_condition.py)."""
    o = _pause_orch(monkeypatch)
    rec = _paused_rec(tmp_path)

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        await o._pause_session(rec, reason="all_accounts_busy")
        await o._pause_session(rec, reason="all_accounts_busy")
        await o._pause_session(rec, reason="crash_cap")

    reasons = [r.fields["reason"] for r in caplog.records
               if r.levelno >= logging.WARNING]
    assert reasons == ["all_accounts_busy", "crash_cap"], reasons


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

    assert o.paused == [("37e13a96", orch_mod._REASSIGN_FAILED_PAUSE_REASON)]
    assert orch_mod._REASSIGN_FAILED_PAUSE_REASON not in orch_mod._TERMINAL_PAUSE_REASONS


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


# ---------------------------------------------------------------------------
# #1857 — three endings under one name, told apart by a number nobody kept
# ---------------------------------------------------------------------------
#
# ``codex.manager.runner_crashed`` named every way a runner can end, and the
# ``exit_code`` that distinguished them lived in a *field*. The sink fingerprints
# a structured record on (source, level, logger, event, exception, frame) and
# never on its fields, and the title it renders normalizes every number to
# ``<n>`` — so a segfault, the #1444 client latch and the app-server exiting 0 by
# itself were one issue, and 42 occurrences of the harmless one meant a real
# crash would have arrived as "1 more occurrence". The #1767 / #1778 shape once
# more: an expected, handled outcome reported as a fault, in a loop.
#
# The condition therefore has to live in the event NAME, and what it cost — a
# lost turn — in the level.

_MGR_LOGGER = logging.getLogger("server.codex.manager")


def _ending(exit_code: int, *, interrupted: int = 0):
    return mgr_mod.classify_runner_ending(
        exit_code=exit_code, interrupted_turns=interrupted
    )


def _sink_fingerprint(caplog, report) -> str:
    """The fingerprint the error sink mints for a record classified this way."""
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="server.codex.manager"):
        log_event(_MGR_LOGGER, report.level, report.event,
                  account="chatgpt-tonystein85_gmail.com", generation=1)
    signature = signature_of(caplog.records[-1], source="daemon", host="dobby")
    assert signature is not None, "the sink dropped a record it must count"
    return signature.fingerprint


def test_an_app_server_that_exits_zero_by_itself_is_not_a_crash():
    """The 42 occurrences in #1857: ``exit_code=0``, ``interrupted_turns=0``. The
    process left of its own accord with nothing in flight, and the supervisor
    replaced it before anyone could notice."""
    report = _ending(0)
    assert report.ending is mgr_mod.RunnerEnding.EXITED_CLEAN
    assert report.event == "codex.manager.runner_exited"
    assert report.level == logging.WARNING


def test_a_clean_exit_that_took_a_turn_with_it_is_an_error():
    """The level follows the cost, not the exit status: the same ending that is
    noise while idle is lost work when a turn was running (the 2026-09-19 16:19
    burst on dobby, where ``interrupted_turns`` was 1)."""
    report = _ending(0, interrupted=1)
    assert report.ending is mgr_mod.RunnerEnding.EXITED_CLEAN
    assert report.level == logging.ERROR


@pytest.mark.parametrize("code", [1, 2, 101, 137, -9, -1])
def test_an_abnormal_death_is_still_a_crash_even_with_nothing_in_flight(code):
    """Guard against widening this into 'a runner ending is never an error'. A
    non-zero status or a signal is a fault whether or not it cost a turn."""
    report = _ending(code)
    assert report.ending is mgr_mod.RunnerEnding.CRASHED
    assert report.event == "codex.manager.runner_crashed"
    assert report.level == logging.ERROR


def test_a_latched_client_keeps_a_name_of_its_own():
    """#1444: the connection died while the process lived. Diagnosed nothing like
    a crash, and must not arrive in the same issue as one."""
    report = _ending(mgr_mod.CLIENT_CLOSED_EXIT_CODE)
    assert report.ending is mgr_mod.RunnerEnding.CLIENT_CLOSED
    assert report.event == "codex.manager.runner_client_closed"
    assert report.level == logging.ERROR


def test_each_ending_files_as_its_own_issue(caplog):
    """The defect in #1857 was collision, so assert its absence at the sink — the
    only place that decides what is one issue — rather than trusting the names."""
    reports = [_ending(0), _ending(0, interrupted=1), _ending(1),
               _ending(mgr_mod.CLIENT_CLOSED_EXIT_CODE)]
    fingerprints = [_sink_fingerprint(caplog, r) for r in reports]
    assert len(set(fingerprints)) == len(fingerprints), (
        f"endings still share an issue: {list(zip([r.event for r in reports], fingerprints))}"
    )


def test_one_ending_is_still_one_issue_however_it_is_numbered(caplog):
    """The other half of the split: a crash storm must not mint an issue per exit
    status. Two deaths of the same kind are two occurrences of one condition."""
    assert _sink_fingerprint(caplog, _ending(1)) == _sink_fingerprint(caplog, _ending(137))


# ---------------------------------------------------------------------------
# #1858 — a restart is not a second report of the crash that caused it
# ---------------------------------------------------------------------------
#
# `_handle_exit` already files the fault `classify_runner_ending` named, at the
# level it gave it. The retry loop then logged `codex.manager.runner_restarted`
# unconditionally at WARNING on every successful recovery — the #1485/#1991
# shape once more, this time inside one function: one incident (the exit),
# reported twice (the exit's own record, then the recovery that followed it),
# each a distinct error-sink fingerprint. 34 occurrences, 2026-09-11 → 09-19,
# dobby, account chatgpt-tonystein85_gmail.com.


def test_a_successful_restart_no_longer_mints_its_own_fingerprint(caplog):
    """The record itself, in isolation: below WARNING, so the sink drops it."""
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="server.codex.manager"):
        log_event(_MGR_LOGGER, logging.INFO, "codex.manager.runner_restarted",
                  account="chatgpt-tonystein85_gmail.com", generation=2, attempt=1)
    record = caplog.records[-1]
    assert record.levelno < logging.WARNING
    assert signature_of(record, source="daemon", host="dobby") is None


# ---------------------------------------------------------------------------
# #1485 / #1991 — a request nobody owns is a routing outcome, reported twice
# ---------------------------------------------------------------------------
#
# ``error_signatures`` on dobby recorded the entire defect. ``codex.manager.
# unrouted_approval`` (WARNING, 20 occurrences, 2026-09-09 09:36:39.945 →
# 12:14:09.267, issue #1486) and ``codex.protocol.approval_failed`` (ERROR, 20
# occurrences, 09:36:39.952 → 12:14:09.274, issue #1485) are the SAME twenty
# events, seven milliseconds apart. The manager names the account and the
# method; the dispatcher's catch-all can only reduce a ``CodexManagerError`` —
# a ``RuntimeError`` subclass, so #1861's taxonomy never reached it — to
# ``error=runtime_error``, and file a second, contentless issue for an incident
# already reported. Closing #1486 muted one half; the other kept filing.
#
# The incident is also a designed outcome rather than a fault:
# ``unregister_thread`` drops a thread's sinks BEFORE its turn fence precisely
# so a request landing during teardown finds no owner and fails closed. The
# codebase already had a settled shape for "no sink owns this thread-scoped
# frame" — ``_on_notification`` logs it at DEBUG and counts it. The approval,
# user-input and dynamic-tool siblings are the three that diverged, which is
# why #1991 is the identical pair for ``item/tool/call``.
#
# So the fix is the #1857 statement from the other side: the condition belongs
# in the event NAME, and one condition gets ONE report — by the layer that can
# describe it. The wire answer is deliberately untouched; fail-closed is the
# property these paths exist for, and an unanswered approval must stay one.

_UNROUTED_SURFACES = ("approval", "user_input", "dynamic_tool")


def _unrouted_manager(tmp_path) -> mgr_mod.CodexRunnerManager:
    """A manager with no registered threads — every sink lookup misses.

    The real object rather than a stub: what is under test is the pair of
    records one incident produces across both layers, so both layers have to be
    the production ones.
    """

    async def _creds(account: str):  # pragma: no cover - no runner is started
        raise AssertionError("no runner is started in these tests")

    return mgr_mod.CodexRunnerManager(
        availability=CodexAvailability(available=True, binary_path="/usr/bin/codex"),
        codex_home_base=tmp_path,
        credential_supplier=_creds,
    )


def _server_request(surface: str) -> tuple[str, dict]:
    """The wire frame for one of the three server-request surfaces."""
    if surface == "approval":
        return (
            schema.METHOD_COMMAND_EXECUTION_APPROVAL,
            {"itemId": "i1", "threadId": "ghost", "turnId": "u1", "startedAtMs": 1,
             "command": "ls"},
        )
    if surface == "user_input":
        return (
            schema.METHOD_ITEM_TOOL_REQUEST_USER_INPUT,
            {"threadId": "ghost", "turnId": "u1", "itemId": "i1", "isBlocking": True,
             "questions": [{"id": "q1", "header": "h", "question": "why?"}]},
        )
    return (
        schema.METHOD_ITEM_TOOL_CALL,
        {"threadId": "ghost", "turnId": "u1", "callId": "c1", "tool": "list_sessions",
         "arguments": {}},
    )


async def _dispatch_to_a_dead_thread(manager, surface: str) -> dict:
    """Send one server request for a thread nobody owns; return the wire reply.

    Wired exactly as ``CodexRunnerManager._start_runner`` wires it, because the
    handler identity is what decides which of the two records gets emitted.
    """
    method, params = _server_request(surface)
    async with FakeCodexAppServer() as fake:
        client = codex_protocol.CodexAppServerClient(
            fake.transport,
            external_auth_enabled=False,
            request_timeout_s=2.0,
            approval_handler=partial(manager._on_approval, "acct"),
            user_input_handler=partial(manager._on_user_input, "acct"),
            dynamic_tool_handler=partial(manager._on_dynamic_tool, "acct"),
        )
        await client.start()
        await asyncio.gather(client.initialize(), fake.serve_initialize())
        try:
            request_id = fake.request(method, params)
            return await fake.receive_reply(request_id)
        finally:
            await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", _UNROUTED_SURFACES)
async def test_one_unrouted_request_files_one_issue_not_two(caplog, tmp_path, surface):
    """The #1485 defect itself: two WARNING+ records for one incident.

    Counted across BOTH loggers, because the sink does not care which module a
    record came from — two records above the capture threshold are two
    fingerprints, and two fingerprints are two issues.
    """
    manager = _unrouted_manager(tmp_path)

    with caplog.at_level(logging.DEBUG):
        await _dispatch_to_a_dead_thread(manager, surface)

    reported = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and r.name in ("server.codex.manager", "server.codex.protocol")
    ]
    assert len(reported) == 1, (
        "one incident reported twice: "
        f"{[(r.name, getattr(r, 'event', None)) for r in reported]}"
    )
    # The surviving report is the one that can name the account and the method.
    assert reported[0].name == "server.codex.manager"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", _UNROUTED_SURFACES)
async def test_an_unrouted_request_is_still_answered_fail_closed(tmp_path, surface):
    """Behaviour is unchanged — only the reporting is. An approval nobody owns
    must never become an allow, and a tool call nobody owns must never come back
    as a result the model would reason from."""
    reply = await _dispatch_to_a_dead_thread(_unrouted_manager(tmp_path), surface)

    assert "result" not in reply
    assert reply["error"]["code"] == codex_protocol.JSONRPC_INTERNAL_ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", _UNROUTED_SURFACES)
async def test_the_dispatcher_still_names_the_surface_it_could_not_route(
        caplog, tmp_path, surface):
    """Dropping the ERROR must not drop the breadcrumb: the dispatcher's own
    trail still has to show which request it declined, next to the
    ``*_answered`` line it emits for every one it served."""
    manager = _unrouted_manager(tmp_path)

    with caplog.at_level(logging.DEBUG, logger="server.codex.protocol"):
        await _dispatch_to_a_dead_thread(manager, surface)

    events = [getattr(r, "event", None) for r in caplog.records]
    assert f"codex.protocol.{surface}_unrouted" in events, events


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", _UNROUTED_SURFACES)
async def test_a_genuine_handler_fault_is_still_an_error(caplog, tmp_path, surface):
    """Guard against widening this into 'a failed server request is never an
    error'. A handler that raises for any other reason is a bug in this process,
    and the record that says so is the only one there is."""
    manager = _unrouted_manager(tmp_path)

    async def _boom(*args, **kwargs):
        raise AttributeError("genuine bug")

    manager.register_thread(
        "acct", "ghost", lambda n: None,
        approvals=_boom, user_input=_boom, dynamic_tools=_boom)

    with caplog.at_level(logging.DEBUG, logger="server.codex.protocol"):
        reply = await _dispatch_to_a_dead_thread(manager, surface)

    assert reply["error"]["code"] == codex_protocol.JSONRPC_INTERNAL_ERROR
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [getattr(r, "event", None) for r in errors] == [
        f"codex.protocol.{surface}_failed"
    ]


def test_the_two_records_would_have_been_two_issues(caplog):
    """Why 'report once' is the fix and not merely tidier: the sink fingerprints
    on (source, level, logger, event, …), so the manager's WARNING and the
    dispatcher's ERROR could never have been folded into one issue — which is
    exactly how muting #1486 left #1485 filing."""
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        log_event(_MGR_LOGGER, logging.WARNING, "codex.manager.unrouted_approval",
                  account="chatgpt-tonystein85_gmail.com", method="x")
        log_event(logging.getLogger("server.codex.protocol"), logging.ERROR,
                  "codex.protocol.approval_failed", method="x", error="runtime_error")

    fingerprints = {
        signature_of(record, source="daemon", host="rocky").fingerprint
        for record in caplog.records
    }
    assert len(fingerprints) == 2
