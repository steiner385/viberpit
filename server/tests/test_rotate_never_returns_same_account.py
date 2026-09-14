"""Rotation must never hand a session back the account it is rotating away from.

Production incident 2026-09-08. Sessions logged, repeatedly:

    reassign.complete session=… tony_steinonline.net→tony_steinonline.net
                                reason=stuck_session_sweep
    recover.paused session=… reason=rotation_cap

`_rotate_away` correctly excluded the current account, but
`pick_account_detailed`'s tiered fallback dropped the *whole* exclude set on its
first relaxation step. The failing account was still under the 100% ceiling
(Anthropic 429s before reported usage reaches 100%, and the cached snapshot is up
to 60s stale), so with the exclusion gone it scored best and came straight back.
Each no-op rotation burned one of the 3-per-300s rotation budget; on the fourth
the session parked as `rotation_cap`, which `is_usage_stalled` renders as "parked
for lack of usage headroom" — while another account sat at 46% weekly.

The contract these tests pin:
  * an account with headroom is used (rotation still works);
  * when only the excluded account has headroom, rotation PAUSES with
    `all_accounts_busy` and an earliest-reset, rather than rotating A→A.
"""
import asyncio

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord
from server.sessions import orchestrator as orch_mod
from server.sessions.model_policy import quota_model
from ccswitch.selector import AllAccountsBusy, Picked


CURRENT = "acct-rate-limited"
FREE = "acct-with-headroom"
# An explicitly tiered model, so the expected quota key is deterministic ("sonnet")
# rather than whatever fleet default a host's settings.json would make "default"
# resolve to.
MODEL = "sonnet"


def _orch():
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 1000.0
    o.calls = []

    async def spy_reassign(record, new_account, *, reason, **kw):
        o.calls.append(("reassign", record.id, record.account, new_account, reason))

    async def spy_pause(record, *, reason, earliest_reset=None):
        o.calls.append(("pause", record.id, reason, earliest_reset))

    async def spy_persist_pin(sid, pinned):
        return None

    o._reassign = spy_reassign
    o._pause_session = spy_pause
    o._persist_pin = spy_persist_pin
    o.live_session_load = lambda: {}
    o._recovery.quarantined = lambda now=None: frozenset()
    return o


def _record(o=None, account=CURRENT, status="needs_attention", model=MODEL):
    rec = SessionRecord(id="sess-1", name="n", account=account,
                        engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp/x", status=status, model=model)
    if o is not None:
        o._records["sess-1"] = rec
        o._drivers["sess-1"] = object()
    return rec


def _selector(monkeypatch, available: set[str], earliest=None):
    """Stand in for select_account: honours `exclude` as a hard filter, which is
    exactly the contract under test. Raises AllAccountsBusy when nothing is left."""
    seen = {}

    def fake(exclude=frozenset(), load=None, *, reserve=True,
             soft_exclude=frozenset(), engine=None, model=None):
        seen["exclude"] = frozenset(exclude)
        seen["soft_exclude"] = frozenset(soft_exclude)
        seen["model"] = model
        for name in sorted(available):
            if name not in exclude:
                return Picked(name=name, optimistic=False)
        raise AllAccountsBusy(earliest, "at_ceiling")

    monkeypatch.setattr(orch_mod, "select_account", fake)
    return seen


def test_rotate_away_moves_to_the_account_with_headroom(monkeypatch):
    o = _orch()
    _selector(monkeypatch, {CURRENT, FREE})
    rec = _record(o)
    asyncio.run(o._rotate_away(rec, reason="rate_limit"))
    assert o.calls == [("reassign", "sess-1", CURRENT, FREE, "rate_limit")]


def test_rotate_away_pauses_rather_than_returning_the_failing_account(monkeypatch):
    """The regression. Only the just-failed account has headroom, so there is
    nowhere to go: park with all_accounts_busy, never reassign A→A."""
    from datetime import datetime, timezone
    reset = datetime(2026, 9, 8, 21, 0, tzinfo=timezone.utc)
    o = _orch()
    _selector(monkeypatch, {CURRENT}, earliest=reset)
    rec = _record(o)
    asyncio.run(o._rotate_away(rec, reason="rate_limit"))
    assert o.calls == [("pause", "sess-1", "all_accounts_busy", reset)]
    assert not any(c[0] == "reassign" for c in o.calls)


def test_rotate_away_passes_current_hard_and_quarantine_soft(monkeypatch):
    """Quarantine must stay relaxable; the current account must not."""
    o = _orch()
    o._recovery.quarantined = lambda now=None: frozenset({"acct-quarantined"})
    seen = _selector(monkeypatch, {CURRENT, FREE})
    rec = _record(o)
    asyncio.run(o._rotate_away(rec, reason="rate_limit"))
    assert seen["exclude"] == frozenset({CURRENT})
    assert seen["soft_exclude"] == frozenset({"acct-quarantined"})
    # …and the pick is made for the model THIS session runs. Without it every
    # window meters the choice, so a Sonnet session is rotated off (or refused)
    # an account that is only out of Fable.
    assert seen["model"] == quota_model(rec.model, engine=rec.engine) == MODEL


def test_rotate_to_best_pauses_rather_than_returning_the_same_account(monkeypatch):
    """The manual 'move to next best account' menu item had the same defect —
    the user clicked rotate and the session stayed put."""
    o = _orch()
    _selector(monkeypatch, {CURRENT})
    rec = _record(o, status="limited")
    rec.paused_reason = "all_accounts_busy"
    result = asyncio.run(o.rotate_to_best("sess-1"))
    assert result == "skipped:all_accounts_busy"
    assert not any(c[0] == "reassign" for c in o.calls)


def test_rotate_to_best_moves_when_another_account_has_headroom(monkeypatch):
    o = _orch()
    seen = _selector(monkeypatch, {CURRENT, FREE})
    rec = _record(o, status="limited")
    asyncio.run(o.rotate_to_best("sess-1"))
    assert ("reassign", "sess-1", CURRENT, FREE, "manual_rotate") in [
        (c[0], c[1], c[2], c[3], c[4]) for c in o.calls if c[0] == "reassign"]
    # The manual rotate picks for the session's own model too (same reason as
    # _rotate_away's assertion above).
    assert seen["model"] == quota_model(rec.model, engine=rec.engine) == MODEL
