"""One parked session is ONE error condition, however many reasons it cycles through.

Incident (2026-09-15/16, dobby + rocky). A session that could not be rebuilt was
retried by *two* independent loops — the rotation monitor (``_restart_driverless``,
trigger ``weekly limit reached``) and the stuck-session sweep (trigger
``stuck_session_sweep``) — and ``_reassign_locked`` recorded each failure as
``reassign_failed:<trigger>``. That produced two defects at once:

* **Three issues for one line.** ``server.obs.error_signature.signature_of``
  fingerprints a plain record on ``normalize(message)``, and normalization only
  masks the shapes it recognizes (uuids, quoted strings, hex runs, ints). A reason
  slug survives intact, so ``"recover.paused session=%s reason=%s"`` mints a fresh
  fingerprint — and therefore a separately filed issue — per reason:
  #1766 ``reassign_failed:stuck_session_sweep``, #1780 ``reassign_failed:weekly
  limit reached``, #1843 ``rotation_cap``. This is #1531/#1875 and #1533/#1877
  again, which is the case ``log_event_throttled`` was written for.

* **A throttle defeated by its own freshness rule.** ``_pause_session`` called
  ``_PAUSE_LOG.forget()`` on any change of reason, so two loops alternating
  reasons on one session forgot each other's window on every tick and every
  emission reported at WARNING: 161 records in 2.8 h for session ``331f7d98``
  against a 6-hour window that should cap one key at four a day.

The two halves are deliberately separate — identity wants to be coarse (one event),
pacing wants to be fine (per session+reason) — so both are pinned here.
"""
from __future__ import annotations

import ast
import inspect
import logging
from pathlib import Path

import pytest

from server.obs.error_signature import signature_of
from server.sessions.engine import AgentEngine
from server.sessions.usage_stall import USAGE_STALL_REASONS
from server.sessions.types import SessionRecord
import server.sessions.orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _pause_orch():
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o.events = []
    o._publish_event = o.events.append

    async def persist(session_id, status):
        return None

    o._persist_status = persist
    orch_mod._PAUSE_LOG.clear()
    return o


def _rec(tmp_path, sid="331f7d98"):
    return SessionRecord(id=sid, name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                         engine_session_id="c1", cols=80, rows=24, cwd=str(tmp_path))


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def _fingerprints(records):
    return {
        signature_of(r, source="daemon", host="dobby").fingerprint
        for r in records
    }


# ---------------------------------------------------------------------------
# Identity: every pause reason is the same condition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_pause_reason_is_one_error_condition(caplog, tmp_path):
    """#1766, #1780 and #1843 are one call site split three ways by a reason slug."""
    o = _pause_orch()

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        for i, reason in enumerate((
                "reassign_failed:stuck_session_sweep",
                "reassign_failed:weekly limit reached",
                "rotation_cap",
                "all_accounts_busy",
        )):
            await o._pause_session(_rec(tmp_path, sid=f"3ba9ff3{i}"), reason=reason)

    prints = _fingerprints(_warnings(caplog))
    assert len(prints) == 1, (
        f"{len(prints)} fingerprints — one stuck session files that many issues")


@pytest.mark.asyncio
async def test_the_reason_still_rides_the_record_the_sink_files(caplog, tmp_path):
    """Collapsing identity must not collapse *content*: the reason is the one thing
    a reader of the filed issue needs, so it has to survive as a structured field."""
    o = _pause_orch()

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        await o._pause_session(_rec(tmp_path), reason="rotation_cap")

    record = _warnings(caplog)[0]
    sig = signature_of(record, source="daemon", host="dobby")
    assert record.fields["reason"] == "rotation_cap"
    assert record.fields["session_id"] == "331f7d98"
    assert "rotation_cap" in sig.title, sig.title


# ---------------------------------------------------------------------------
# Severity: an expected capacity wait is not an anomaly (#2330)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_accounts_busy_is_journaled_below_warning(caplog, tmp_path):
    """Every account at its limit is self-healing capacity exhaustion: the sink files
    every WARNING as an issue, so it must not reach WARNING — but the reason still
    rides the INFO record, and the dashboard's `paused` event is unaffected."""
    o = _pause_orch()

    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        await o._pause_session(_rec(tmp_path), reason="all_accounts_busy")

    assert _warnings(caplog) == []
    info = [r for r in caplog.records if r.getMessage().startswith("session.paused")]
    assert len(info) == 1 and info[0].levelno == logging.INFO
    assert info[0].fields["reason"] == "all_accounts_busy"
    assert [e["reason"] for e in o.events if e["type"] == "paused"] == ["all_accounts_busy"]


@pytest.mark.asyncio
async def test_anomaly_pause_reasons_still_warn(caplog, tmp_path):
    o = _pause_orch()

    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        await o._pause_session(_rec(tmp_path), reason="crash_cap")

    records = _warnings(caplog)
    assert len(records) == 1 and records[0].getMessage().startswith("session.paused")


def test_expected_pause_reasons_are_a_subset_of_the_vocabulary():
    assert orch_mod._EXPECTED_PAUSE_REASONS
    assert orch_mod._EXPECTED_PAUSE_REASONS <= orch_mod.PAUSE_REASONS


# ---------------------------------------------------------------------------
# Pacing: two retry loops must not forget each other's window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alternating_retry_loops_do_not_reopen_each_others_window(
        caplog, tmp_path):
    """The 161-records-in-2.8h flood: the sweep and the rotation monitor park the
    same session under different reasons, and `forget()` re-armed both every tick."""
    o = _pause_orch()
    rec = _rec(tmp_path)

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        for _ in range(80):
            await o._pause_session(rec, reason="reassign_failed:stuck_session_sweep")
            await o._pause_session(rec, reason="reassign_failed:weekly limit reached")

    reported = _warnings(caplog)
    assert len(reported) <= 2, (
        f"{len(reported)} records for one parked session flipping between two loops")


@pytest.mark.asyncio
async def test_a_second_session_is_not_silenced_by_the_first(caplog, tmp_path):
    """The window is per session+reason: a newly stuck session is news even while
    another one is mid-window on the identical reason."""
    o = _pause_orch()

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        await o._pause_session(_rec(tmp_path, sid="aaaaaaa1"), reason="rotation_cap")
        await o._pause_session(_rec(tmp_path, sid="aaaaaaa2"), reason="rotation_cap")

    assert len(_warnings(caplog)) == 2


# ---------------------------------------------------------------------------
# Cardinality: a pause reason is a token, never a sentence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_reassign_parks_under_one_reason_whatever_triggered_it(
        monkeypatch, tmp_path):
    """``reassign_failed:<trigger>`` made the *cause* carry the *trigger*, so the
    same failure parked under as many reasons as there are callers of _reassign."""
    parked: list[str] = []

    for trigger in ("stuck_session_sweep", "weekly limit reached", "process_crash"):
        o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
        o._clock = lambda: 1000.0
        o._copilot = None
        o._suggester = None
        o._session_factory = lambda sid: lambda *a, **k: None
        o._install_driver = lambda sid, drv: o._drivers.__setitem__(sid, drv)

        async def spy_pause(record, reason, earliest_reset=None):
            parked.append(reason)

        o._pause_session = spy_pause
        rec = SessionRecord(id="s1", name="n", account="a",
                            engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                            cols=80, rows=24, cwd=str(tmp_path))
        o._records[rec.id] = rec

        class _NewDriver:
            engine_session_id = None
            mode = "interactive"

            async def start(self): raise RuntimeError("CLI exited 1")
            async def interrupt(self): pass
            async def aclose(self): pass
            async def set_model(self, m): pass

        monkeypatch.setattr(orch_mod.SessionOrchestrator, "_create_driver",
                            lambda self, **kw: _NewDriver())

        async def allow(session_id, *, why, adopt=False):
            return True

        o._claim_session = allow
        o._publish_event = lambda e: None
        _stub_env(monkeypatch)

        assert await o._reassign(rec, "b", reason=trigger) == "failed"

    assert parked == [orch_mod._REASSIGN_FAILED_PAUSE_REASON] * 3, parked


def test_the_trigger_still_reaches_the_journal():
    """Dropping the trigger from the *reason* is only safe because the reassign
    record still names it — otherwise "which loop kept retrying this?" becomes
    unanswerable from the journal."""
    src = inspect.getsource(SessionOrchestrator._reassign_locked)
    assert 'log.exception("reassign.error session=%s"' not in src, (
        "the generic failure branch must carry the trigger too")
    assert src.count("trigger=%s") >= 4, (
        "every reassign failure branch names the loop that triggered it")


def _pause_reason_expressions() -> list[tuple[int, ast.expr]]:
    """Every expression the orchestrator can hand ``_pause_session`` as its reason.

    Resolved one level through the local ``paused_reason`` variable on purpose: the
    defect shipped as ``paused_reason = f"reassign_failed:{reason}"`` three lines
    above the call, so a guard that only reads the call site's ``reason=`` kwarg
    sees a harmless ``ast.Name`` and passes on the broken code.
    """
    tree = ast.parse(Path(orch_mod.__file__).read_text())
    bound: dict[str, list[tuple[int, ast.expr]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                bound.setdefault(target.id, []).append((node.lineno, node.value))

    out: list[tuple[int, ast.expr]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "_pause_session"):
            continue
        for kw in node.keywords:
            if kw.arg != "reason":
                continue
            if isinstance(kw.value, ast.Name) and kw.value.id in bound:
                out.extend(bound[kw.value.id])
            else:
                out.append((node.lineno, kw.value))
    return out


def test_no_pause_reason_is_built_by_interpolation():
    """A ``LogThrottle`` key must be bounded in cardinality *by construction*
    (server/obs/logdedupe.py). ``_pause_session``'s reason is one — and it is also
    the error sink's title and the dashboard's parked-cause copy — so it must be a
    literal or a module constant, never a sentence assembled at the call site."""
    found = _pause_reason_expressions()
    assert found, "guard found no _pause_session reasons to check — it cannot fail"
    offenders = [
        f"line {lineno}: {ast.unparse(expr)}"
        for lineno, expr in found
        if not isinstance(expr, (ast.Constant, ast.Name, ast.Attribute))
    ]
    assert offenders == [], (
        "interpolated pause reason — unbounded throttle keys and one filed issue "
        f"per distinct value: {offenders}")


def test_every_pause_reason_is_declared_in_the_one_vocabulary():
    """``PAUSE_REASONS`` is the single source of truth for what ``paused_reason``
    may hold; the terminal set and the usage-stall set are subsets of it. A new
    reason that skips it is one the dashboard has no copy for."""
    literals = {
        expr.value for _, expr in _pause_reason_expressions()
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str)
    }
    assert literals, "guard found no literal pause reasons — it cannot fail"
    assert literals <= orch_mod.PAUSE_REASONS, sorted(literals - orch_mod.PAUSE_REASONS)
    assert orch_mod._TERMINAL_PAUSE_REASONS <= orch_mod.PAUSE_REASONS
    assert USAGE_STALL_REASONS <= orch_mod.PAUSE_REASONS


def _stub_env(monkeypatch):
    class _MockDB:
        async def execute(self, *a): pass

    class _FakeDbCtx:
        async def __aenter__(self): return _MockDB()
        async def __aexit__(self, *a): pass

    monkeypatch.setattr(orch_mod, "db_session", lambda: _FakeDbCtx())
    monkeypatch.setattr(orch_mod, "compose", lambda acct: Path("/fake/cfg"))
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)
