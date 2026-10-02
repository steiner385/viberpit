import time

import pytest

from server.sessions.engine import AgentEngine


@pytest.mark.asyncio
async def test_reassign_reapplies_persisted_model(monkeypatch):
    # Capture the set_model calls on the rebuilt driver.
    from server.sessions import orchestrator as orch_mod

    calls = {}

    class _NewDriver:
        def __init__(self, *a, **k):
            self.cwd = "/tmp"; self.mode = "interactive"; self.identity = None
            self.engine_session_id = "c1"
        async def start(self): pass
        async def send_prompt(self, p): pass
        async def set_model(self, m): calls["model"] = m

    class _OldDriver:
        cwd = "/tmp"; mode = "interactive"; identity = None; engine_session_id = "c1"
        async def interrupt(self): pass
        async def aclose(self): pass

    # Stub the collaborators _reassign reaches for.
    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_create_driver", lambda self, **kw: _NewDriver(resume=kw.get("resume"), identity=kw.get("identity")))
    monkeypatch.setattr(orch_mod, "compose", lambda a: "/cfg")
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)

    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.engine import AgentEngine
    from server.sessions.types import SessionRecord
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", model="sonnet")
    orch._records = {"s1": rec}
    orch._drivers = {"s1": _OldDriver()}
    orch._last_prompt = {}
    orch._turn_restart_at = {}
    orch._swapping = {}
    orch._clock = time.time
    orch._registry = None
    orch._suggester = None
    orch._session_factory = lambda sid: None
    orch._persist_engine_session_id = lambda *a, **k: None
    orch._persist_status = lambda *a, **k: None
    orch._handle_recoverable_failure = lambda *a, **k: None
    # Stub DB write + event publish so we test only the model re-apply.
    async def _noop_db(self, *a, **k): pass
    orch._publish_event = lambda e: None
    monkeypatch.setattr(orch_mod, "db_session", lambda: _NullCtx())

    await orch._reassign(rec, "b", reason="manual")
    assert calls["model"] == "sonnet"


@pytest.mark.asyncio
async def test_reassign_fires_driver_replaced_hook(monkeypatch):
    """A recovery reassign must notify on_driver_replaced with the NEW driver so the
    daemon RPC layer restarts the per-session pump (an attached viewer would otherwise
    freeze until reattach — the whole point of the hook)."""
    from server.sessions import orchestrator as orch_mod

    class _NewDriver:
        def __init__(self, *a, **k):
            self.cwd = "/tmp"; self.mode = "interactive"; self.identity = None
            self.engine_session_id = "c1"
        async def start(self): pass
        async def send_prompt(self, p): pass
        async def set_model(self, m): pass

    class _OldDriver:
        cwd = "/tmp"; mode = "interactive"; identity = None; engine_session_id = "c1"
        async def interrupt(self): pass
        async def aclose(self): pass

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_create_driver", lambda self, **kw: _NewDriver(resume=kw.get("resume"), identity=kw.get("identity")))
    monkeypatch.setattr(orch_mod, "compose", lambda a: "/cfg")
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)
    monkeypatch.setattr(orch_mod, "db_session", lambda: _NullCtx())

    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.types import SessionRecord
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", model=None)
    orch._records = {"s1": rec}
    old = _OldDriver()
    orch._drivers = {"s1": old}
    orch._last_prompt = {}
    orch._turn_restart_at = {}
    orch._swapping = {}
    orch._clock = time.time
    orch._registry = None
    orch._suggester = None
    orch._session_factory = lambda sid: None
    orch._persist_engine_session_id = lambda *a, **k: None
    orch._persist_status = lambda *a, **k: None
    orch._handle_recoverable_failure = lambda *a, **k: None
    orch._publish_event = lambda e: None

    captured = []
    orch.on_driver_replaced = lambda sid, drv: captured.append((sid, drv))

    await orch._reassign(rec, "b", reason="manual")

    assert len(captured) == 1
    sid, new_driver = captured[0]
    assert sid == "s1"
    assert isinstance(new_driver, _NewDriver)          # the swapped-in driver, not the old one
    assert new_driver is not old
    assert orch._drivers["s1"] is new_driver           # dict updated in lockstep with the hook


class _NullCtx:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def execute(self, *a, **k): return None


@pytest.mark.asyncio
async def test_reassign_rotated_event_carries_rev_token(monkeypatch):
    """The `rotated` event must carry the same `revAt` token a subsequent sessions read
    (record.rev_at, the single source of truth also used by the `status` event and the
    HTTP session list) returns — bumped by the rotation itself. Without this the client's
    rev-guarded reconcile (web/src/session/reconcile.ts revIsNewer/shouldApplyRev) has no
    way to tell the WS account patch is newer than a poll response snapshotted just before
    the rotation persisted, so the stale poll silently reverts the account badge."""
    from datetime import datetime, timedelta
    from server.sessions import orchestrator as orch_mod
    from server.time_iso import iso_utc

    class _NewDriver:
        def __init__(self, *a, **k):
            self.cwd = "/tmp"; self.mode = "interactive"; self.identity = None
            self.engine_session_id = "c1"
        async def start(self): pass
        async def send_prompt(self, p): pass
        async def set_model(self, m): pass

    class _OldDriver:
        cwd = "/tmp"; mode = "interactive"; identity = None; engine_session_id = "c1"
        async def interrupt(self): pass
        async def aclose(self): pass

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_create_driver", lambda self, **kw: _NewDriver(resume=kw.get("resume"), identity=kw.get("identity")))
    monkeypatch.setattr(orch_mod, "compose", lambda a: "/cfg")
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)
    monkeypatch.setattr(orch_mod, "db_session", lambda: _NullCtx())

    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.types import SessionRecord
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    stale_rev = datetime.utcnow() - timedelta(minutes=5)
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", model=None, updated_at=stale_rev)
    orch._records = {"s1": rec}
    orch._drivers = {"s1": _OldDriver()}
    orch._last_prompt = {}
    orch._turn_restart_at = {}
    orch._swapping = {}
    orch._clock = time.time
    orch._registry = None
    orch._suggester = None
    orch._session_factory = lambda sid: None
    orch._persist_engine_session_id = lambda *a, **k: None
    orch._persist_status = lambda *a, **k: None
    orch._handle_recoverable_failure = lambda *a, **k: None

    published = []
    orch._publish_event = lambda e: published.append(e)

    await orch._reassign(rec, "b", reason="manual")

    assert len(published) == 1
    event = published[0]
    assert event["type"] == "rotated"
    assert event.get("revAt"), "rotated event must carry a revAt token"
    # Single source of truth: the token equals what a subsequent sessions read
    # (record.rev_at, serialized identically by server/api/sessions.py) returns — not a
    # parallel freshness mechanism invented for this event.
    assert event["revAt"] == rec.rev_at
    # And it must be strictly newer than the pre-rotation rev so the client's
    # revIsNewer/shouldApplyRev comparisons keep the WS patch over a stale in-flight poll.
    assert event["revAt"] > iso_utc(stale_rev)


@pytest.mark.asyncio
async def test_reassign_model_reapply_failure_still_completes_the_swap(monkeypatch, caplog):
    """#2572: the target account's CLI refused to confirm the model ("Couldn't confirm
    model ... with the API") AFTER the new driver was started and installed. The swap
    had already happened, yet the bare set_model let the exception reach the generic
    `reassign.error` branch: ERROR logged (auto-filed), session paused reassign_failed
    with a live driver attached, the DB account write and the prompt replay skipped.
    A failed re-apply is a degraded swap, not a failed one — the same rule
    ensure_driver applies to this exact call."""
    import logging
    from server.sessions import orchestrator as orch_mod

    sent = []

    class _NewDriver:
        def __init__(self, *a, **k):
            self.cwd = "/tmp"; self.mode = "interactive"; self.identity = None
            self.engine_session_id = "c1"
        async def start(self): pass
        async def send_prompt(self, p): sent.append(p)
        async def set_model(self, m):
            raise Exception(f'Couldn\'t confirm model "{m}" with the API.')

    class _OldDriver:
        cwd = "/tmp"; mode = "interactive"; identity = None; engine_session_id = "c1"
        async def interrupt(self): pass
        async def aclose(self): pass

    executed = []

    class _RecordingCtx(_NullCtx):
        async def execute(self, stmt, *a, **k):
            executed.append(stmt.compile().params)

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_create_driver", lambda self, **kw: _NewDriver())
    monkeypatch.setattr(orch_mod, "compose", lambda a: "/cfg")
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)
    monkeypatch.setattr(orch_mod, "db_session", lambda: _RecordingCtx())

    from server.sessions.orchestrator import SessionOrchestrator
    from server.sessions.types import SessionRecord
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", model="claude-fable-5-1")
    orch._records = {"s1": rec}
    orch._drivers = {"s1": _OldDriver()}
    orch._last_prompt = {"s1": "keep going"}
    orch._turn_restart_at = {}
    orch._swapping = {}
    orch._clock = time.time
    orch._registry = None
    orch._suggester = None
    orch._session_factory = lambda sid: None
    orch._persist_engine_session_id = lambda *a, **k: None
    orch._persist_status = lambda *a, **k: None
    orch._handle_recoverable_failure = lambda *a, **k: None
    paused = []

    async def _spy_pause(record, *, reason, **k):
        paused.append(reason)
    orch._pause_session = _spy_pause
    published = []
    orch._publish_event = lambda e: published.append(e)

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        outcome = await orch._reassign(rec, "b", reason="manual")

    assert outcome == "rotated"
    assert paused == []
    assert rec.account == "b"
    assert any(p.get("account_name") == "b" for p in executed), executed
    assert sent == ["keep going"]
    assert [e["type"] for e in published] == ["rotated"]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("set_model" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


# ── #2594: a failed re-apply is retried before the next turn, never silently dropped ──

class _FlakyModelDriver:
    """A live driver whose set_model fails the first ``fail_times`` calls (the 429 a
    rate-limited account returns at resume), then succeeds."""
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.set_model_calls = []
        self.prompts = []
        self.errors = []
    async def set_model(self, m):
        self.set_model_calls.append(m)
        if len(self.set_model_calls) <= self.fail_times:
            raise Exception("API error: 429 This request would exceed your account's "
                            "rate limit. Please try again later · model not changed")
    async def send_prompt(self, text, attachments=None, **kw):
        self.prompts.append(text)
    async def emit_error(self, code, message):
        self.errors.append((code, message))


def _orch_with(driver, rec):
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {rec.id: rec}
    orch._drivers = {rec.id: driver}
    orch._last_prompt = {}
    orch._draining = False
    return orch


def _rec(model="claude-fable-5-1"):
    from server.sessions.types import SessionRecord
    return SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                         engine_session_id="c1", cols=80, rows=24, cwd="/tmp", model=model)


@pytest.mark.asyncio
async def test_failed_reapply_is_retried_before_the_next_turn(caplog):
    """The resume-time 429 is transient. The record keeps the user's model, and the
    next turn re-applies it before the prompt reaches the CLI — so the turn runs on
    the model the session reports, and a recovered blip files nothing."""
    import logging
    drv, rec = _FlakyModelDriver(fail_times=1), _rec()
    orch = _orch_with(drv, rec)

    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        await orch._reapply_model(drv, rec, site="session.resume")
        assert rec.model == "claude-fable-5-1"
        assert rec.model_unapplied is True
        assert await orch.send_prompt("s1", "go") == "sent"

    assert drv.set_model_calls == ["claude-fable-5-1", "claude-fable-5-1"]
    assert drv.prompts == ["go"]
    assert rec.model_unapplied is False
    assert drv.errors == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_retry_failure_tells_the_user_and_still_sends(caplog):
    """When the turn-time retry fails too, the session says so — an error card naming
    the model it could not apply — instead of a log line nobody reads. The prompt is
    still delivered (a failed re-apply never fails a turn), and the retry is not
    repeated on every later turn."""
    import logging
    drv, rec = _FlakyModelDriver(fail_times=2), _rec()
    orch = _orch_with(drv, rec)

    await orch._reapply_model(drv, rec, site="session.resume")
    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        assert await orch.send_prompt("s1", "go") == "sent"
    assert drv.prompts == ["go"]
    assert [c for c, _ in drv.errors] == ["model_not_applied"]
    assert "claude-fable-5-1" in drv.errors[0][1]
    assert any("set_model" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)

    assert await orch.send_prompt("s1", "again") == "sent"
    assert len(drv.set_model_calls) == 2
    assert len(drv.errors) == 1


@pytest.mark.asyncio
async def test_applied_model_sends_without_a_retry():
    drv, rec = _FlakyModelDriver(fail_times=0), _rec()
    orch = _orch_with(drv, rec)
    await orch._reapply_model(drv, rec, site="session.resume")
    assert await orch.send_prompt("s1", "go") == "sent"
    assert drv.set_model_calls == ["claude-fable-5-1"]
    assert rec.model_unapplied is False
