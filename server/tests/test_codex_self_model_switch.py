"""Codex self-directed model switching (#1273).

A Codex session self-assesses in the SAME haiku/sonnet/opus/fable vocabulary a
Claude session does — the marker and the instruction (`model_request.py`) are
engine-neutral on purpose. What differs is entirely server-side: the requested
tier is never dispatched as-is (a Codex model has no such alias); it is mapped
onto the account's live `model/list` reasoning-effort catalog via
`model_pick.tier_to_effort`, keeping the session's current model unchanged.

Three layers, three test groups below:

1. `model_pick.tier_to_effort` — the pure ordinal projection.
2. `SessionOrchestrator._resolve_codex_self_tier` — the live-catalog lookup,
   unit-tested the same way `test_automation_spawn_model.py` tests its
   spawn-time counterpart `_resolve_codex_auto_effort`.
3. `_apply_model_request` / `resolve_model_approval` — the governance
   integration, built on the SAME harness `test_self_model_switch_orchestrator.py`
   uses (a real `SessionOrchestrator`, `_resolve_codex_self_tier` stubbed so
   these stay unit tests of the governance wiring, not of the catalog RPC).
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from server.automations.model_pick import tier_to_effort
from server.sessions.engine import AgentEngine
from server.sessions.self_model_guards import SelfModelState
from server.sessions.types import SessionRecord
from server.tests.test_orchestrator_model_gate import _harness, _seed_account

# ---------------------------------------------------------------------------
# 1. model_pick.tier_to_effort
# ---------------------------------------------------------------------------


def test_tier_to_effort_scales_ordinally_cheapest_first():
    efforts = ("low", "medium", "high")
    assert tier_to_effort("haiku", efforts) == "low"
    assert tier_to_effort("fable", efforts) == "high"


def test_tier_to_effort_clamps_a_finer_pool_than_tiers():
    # Two efforts, four tiers: the ordinal projection buckets the cheaper HALF of
    # tiers onto "low" and the costlier half onto "high" — never past the end of
    # *efforts* — the same clamp assess_codex_effort always had.
    efforts = ("low", "high")
    assert tier_to_effort("haiku", efforts) == "low"
    assert tier_to_effort("sonnet", efforts) == "low"
    assert tier_to_effort("opus", efforts) == "high"
    assert tier_to_effort("fable", efforts) == "high"


def test_tier_to_effort_none_on_empty_efforts_or_unknown_tier():
    assert tier_to_effort("haiku", ()) is None
    assert tier_to_effort("not-a-tier", ("low", "high")) is None


# ---------------------------------------------------------------------------
# 2. SessionOrchestrator._resolve_codex_self_tier
# ---------------------------------------------------------------------------


class _FakeCodexOption:
    def __init__(self, id_, default=False, reasoning_efforts=()):
        self.id = id_
        self.default = default
        self.reasoning_efforts = reasoning_efforts


class _FakeCodexAvailability:
    cli_version = "1.0.0"


class _FakeCodexManager:
    available = True
    availability = _FakeCodexAvailability()

    async def ensure(self, account):
        pass

    def client_for(self, account):
        return object()


class _FakeCodexCatalog:
    def __init__(self, options):
        self._options = options

    async def models(self, account, *, client, cli_version, refresh=False):
        return self._options


def _bare_orchestrator():
    import server.sessions.orchestrator as orch_mod
    return orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)


def _codex_rec(model="gpt-5.1-codex", account="acct"):
    return SessionRecord(
        id="s1", name="n", account=account, engine=AgentEngine.CODEX,
        engine_session_id="c1", cols=80, rows=24, cwd="/tmp", model=model,
        updated_at=datetime.utcnow())


@pytest.mark.asyncio
async def test_resolve_codex_self_tier_scales_the_sessions_own_model():
    orch = _bare_orchestrator()
    orch._codex = _FakeCodexManager()
    orch._codex_model_catalog = _FakeCodexCatalog((
        _FakeCodexOption("gpt-5.1-codex-mini", default=True,
                         reasoning_efforts=("minimal", "low")),
        _FakeCodexOption("gpt-5.1-codex", reasoning_efforts=("low", "medium", "high")),
    ))
    got = await orch._resolve_codex_self_tier(
        _codex_rec(model="gpt-5.1-codex"), "fable")
    assert got == "high"


@pytest.mark.asyncio
async def test_resolve_codex_self_tier_falls_back_to_catalog_default_model():
    orch = _bare_orchestrator()
    orch._codex = _FakeCodexManager()
    orch._codex_model_catalog = _FakeCodexCatalog((
        _FakeCodexOption("gpt-5.1-codex-mini", default=True,
                         reasoning_efforts=("minimal", "low")),
    ))
    # The session's persisted model isn't in the (possibly refreshed) catalog —
    # fall back to whichever option the catalog itself marks default.
    got = await orch._resolve_codex_self_tier(
        _codex_rec(model="unknown-model-id"), "haiku")
    assert got == "minimal"


@pytest.mark.asyncio
async def test_resolve_codex_self_tier_none_without_a_runner():
    orch = _bare_orchestrator()
    orch._codex = None
    got = await orch._resolve_codex_self_tier(_codex_rec(), "haiku")
    assert got is None


@pytest.mark.asyncio
async def test_resolve_codex_self_tier_none_without_an_account():
    orch = _bare_orchestrator()
    orch._codex = _FakeCodexManager()
    got = await orch._resolve_codex_self_tier(_codex_rec(account=None), "haiku")
    assert got is None


@pytest.mark.asyncio
async def test_resolve_codex_self_tier_fails_open_on_catalog_error():
    from server.codex.catalog import ModelCatalogError

    class _BoomCatalog:
        async def models(self, *a, **kw):
            raise ModelCatalogError("app-server unreachable")

    orch = _bare_orchestrator()
    orch._codex = _FakeCodexManager()
    orch._codex_model_catalog = _BoomCatalog()
    got = await orch._resolve_codex_self_tier(_codex_rec(), "haiku")
    assert got is None


@pytest.mark.asyncio
async def test_resolve_codex_self_tier_none_on_empty_catalog():
    orch = _bare_orchestrator()
    orch._codex = _FakeCodexManager()
    orch._codex_model_catalog = _FakeCodexCatalog(())
    got = await orch._resolve_codex_self_tier(_codex_rec(), "haiku")
    assert got is None


# ---------------------------------------------------------------------------
# 3a. _apply_model_request — Codex branch
# ---------------------------------------------------------------------------


class _FakeDriver:
    def __init__(self, req=None):
        self.last_model_request = req

    async def aclose(self):
        pass


def _attach_codex(orch, *, session_id="s1", model="gpt-5.1-codex", account="alpha",
                  req=None):
    rec = SessionRecord(
        id=session_id, name="n", account=account, engine=AgentEngine.CODEX,
        engine_session_id="c1", cols=80, rows=24, cwd="/tmp", model=model,
        updated_at=datetime.utcnow())
    orch._records[session_id] = rec
    orch._drivers[session_id] = _FakeDriver(req)
    return rec


def _patch_decision(monkeypatch, decision):
    async def _decide(db, *, current_model=None, requested_model=None, account=None):
        return decision
    monkeypatch.setattr(
        "server.sessions.model_policy.resolve_self_directed_model_policy", _decide)


def test_codex_downgrade_applies_via_the_live_catalog(tmp_path, monkeypatch):
    """A first request (no tracked baseline) is judged against the assumed
    "sonnet" middle tier: "haiku" is a downgrade, so it auto-applies — resolved
    to an effort, dispatched with the model UNCHANGED, never a bare tier word."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")
        _patch_decision(monkeypatch, "apply")

        async def _run():
            orch = await h.make_orch()
            try:
                req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
                _attach_codex(orch, req=req)

                async def _resolve(rec, tier):
                    assert (rec.id, tier) == ("s1", "haiku")
                    return "low"
                orch._resolve_codex_self_tier = _resolve

                calls = []

                async def _set(session_id, model, *, when="now", identity="system",
                               reasoning_effort=None):
                    calls.append((session_id, model, when, identity, reasoning_effort))
                    return False
                orch.set_model_for_session = _set

                await orch._apply_model_request("s1", req)
                return calls, orch._self_model_state["s1"]
            finally:
                await orch.stop()

        calls, state = asyncio.run(_run())

    # The model rides UNCHANGED; only the effort — resolved from the live
    # catalog — changes. A tier word never reaches the dispatch.
    assert calls == [("s1", "gpt-5.1-codex", "after_turn", "session:self", "low")]
    assert state.auto_applied == 1
    assert state.codex_tier == "haiku"


def test_codex_second_request_is_judged_against_the_tracked_tier_not_the_baseline(
    tmp_path, monkeypatch,
):
    """Once a tier has been applied, THAT is the baseline for direction — not the
    assumed "sonnet" default — so a same-tier repeat is a noop, not a downgrade."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            seen_current = []

            async def _decide(db, *, current_model=None, requested_model=None,
                              account=None):
                seen_current.append(current_model)
                return "apply"
            monkeypatch.setattr(
                "server.sessions.model_policy.resolve_self_directed_model_policy",
                _decide)

            orch = await h.make_orch()
            try:
                req = {"turn_id": "t1", "model": "haiku", "reason": "r"}
                _attach_codex(orch, req=req)
                orch._self_model_state["s1"] = SelfModelState(codex_tier="opus")

                async def _resolve(rec, tier):
                    return "low"
                orch._resolve_codex_self_tier = _resolve

                async def _set(session_id, model, *, when="now", identity="system",
                               reasoning_effort=None):
                    return False
                orch.set_model_for_session = _set

                await orch._apply_model_request("s1", req)
                return seen_current
            finally:
                await orch.stop()

        seen_current = asyncio.run(_run())

    # The direction check ran against the tracked "opus", not the "sonnet" default.
    assert seen_current == ["opus"]


def test_codex_undeliverable_request_is_dropped_not_guessed(tmp_path, monkeypatch):
    """The catalog can't answer (unreachable app-server, empty catalog): drop the
    request rather than dispatch a tier word or a made-up effort."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")
        _patch_decision(monkeypatch, "apply")

        async def _run():
            orch = await h.make_orch()
            try:
                req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
                _attach_codex(orch, req=req)

                async def _resolve(rec, tier):
                    return None
                orch._resolve_codex_self_tier = _resolve

                called = []
                orch.set_model_for_session = lambda *a, **k: called.append(a)

                await orch._apply_model_request("s1", req)
                return called, orch._self_model_state.get("s1")
            finally:
                await orch.stop()

        called, state = asyncio.run(_run())

    assert called == []
    # Never touched: the request never got far enough to count as applied.
    assert state is not None and state.auto_applied == 0


def test_codex_upgrade_parks_without_touching_the_catalog(tmp_path, monkeypatch):
    """A park never needs a resolved effort — the hold stores the TIER, and the
    catalog is only consulted when a human later approves it."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")
        _patch_decision(monkeypatch, "park")

        async def _run():
            orch = await h.make_orch()
            try:
                req = {"turn_id": "t1", "model": "fable", "reason": "hard merge"}
                _attach_codex(orch, req=req)

                async def _boom(rec, tier):
                    raise AssertionError("must not resolve a catalog effort to park")
                orch._resolve_codex_self_tier = _boom

                parked = []

                async def _park(sid, model, reason):
                    parked.append((sid, model, reason))
                orch._park_self_model_request = _park

                await orch._apply_model_request("s1", req)
                return parked
            finally:
                await orch.stop()

        parked = asyncio.run(_run())

    assert parked == [("s1", "fable", "hard merge")]


# ---------------------------------------------------------------------------
# 3b. resolve_model_approval — Codex mid-session hold
# ---------------------------------------------------------------------------


def _attach_codex_hold(orch, *, session_id="s1", model="gpt-5.1-codex",
                       account="alpha", pending="opus"):
    rec = SessionRecord(
        id=session_id, name="n", account=account, engine=AgentEngine.CODEX,
        engine_session_id="c1", cols=80, rows=24, cwd="/tmp", model=model,
        pending_model=pending, pending_model_requested_by="session:self",
        updated_at=datetime.utcnow())
    orch._records[session_id] = rec
    return rec


def test_codex_approval_dispatches_the_resolved_effort_not_the_tier(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            orch = await h.make_orch()
            try:
                rec = _attach_codex_hold(orch, pending="opus")

                async def _resolve(r, tier):
                    assert (r.id, tier) == ("s1", "opus")
                    return "high"
                orch._resolve_codex_self_tier = _resolve

                calls = []

                class _Driver:
                    async def set_model(self, model, *, effort=None):
                        calls.append((model, effort))
                orch._drivers["s1"] = _Driver()

                await orch.resolve_model_approval("s1", "approve", identity="tony")
                return calls, rec
            finally:
                await orch.stop()

        calls, rec = asyncio.run(_run())

    # The model rides unchanged; only the effort dial moved.
    assert calls == [("gpt-5.1-codex", "high")]
    assert rec.reasoning_effort == "high"
    assert rec.model == "gpt-5.1-codex"
    assert rec.pending_model is None


def test_codex_approval_with_an_unreadable_catalog_clears_the_hold_but_applies_nothing(
    tmp_path,
):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            orch = await h.make_orch()
            try:
                rec = _attach_codex_hold(orch, pending="opus")

                async def _resolve(r, tier):
                    return None
                orch._resolve_codex_self_tier = _resolve

                calls = []

                class _Driver:
                    async def set_model(self, model, *, effort=None):
                        calls.append((model, effort))
                orch._drivers["s1"] = _Driver()

                await orch.resolve_model_approval("s1", "approve", identity="tony")
                return calls, rec
            finally:
                await orch.stop()

        calls, rec = asyncio.run(_run())

    assert calls == []
    assert rec.reasoning_effort is None
    assert rec.pending_model is None


def test_codex_denial_clears_the_hold_without_touching_the_driver(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            orch = await h.make_orch()
            try:
                rec = _attach_codex_hold(orch, pending="opus")

                async def _boom(r, tier):
                    raise AssertionError("a denial must never resolve the catalog")
                orch._resolve_codex_self_tier = _boom

                class _Driver:
                    async def set_model(self, model, *, effort=None):
                        raise AssertionError("a denial must never touch the driver")
                orch._drivers["s1"] = _Driver()

                await orch.resolve_model_approval("s1", "deny", identity="tony")
                return rec
            finally:
                await orch.stop()

        rec = asyncio.run(_run())

    assert rec.pending_model is None
    assert rec.reasoning_effort is None
