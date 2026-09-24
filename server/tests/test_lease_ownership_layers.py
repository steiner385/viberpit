"""Session lease ownership: choke-point fence, build-window grace, self-healing
reconcile (spec docs/superpowers/specs/2026-09-16-session-lease-ownership-design.md).

Companion to test_session_lease.py (the lease protocol and gate) and
test_recovery_actor_ownership.py (per-caller fences). These pin the three layers
that make the 331f7d98 split-brain (#1824, #1826) structurally impossible."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from server.sessions import session_lease
from server.sessions.orchestrator import SessionOrchestrator, SPAWN_WAIT_S
from server.tests.test_host_partitioned_ownership import _record

BOTH = frozenset({"dobby", "rocky"})


@pytest.fixture
def primary(monkeypatch):
    import server.sessions.orchestrator as mod
    monkeypatch.setattr(mod, "daemon_role", lambda: "primary")
    monkeypatch.setattr(mod, "self_host_name", lambda: "dobby")
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    orch._drivers = {}
    orch._enabled_host_names = AsyncMock(return_value=BOTH)
    return orch


@pytest.mark.parametrize("host,enabled,expected", [
    ("dobby", BOTH, True),        # my stamp
    (None, BOTH, True),           # legacy / primary automation spawn
    ("rocky", BOTH, False),       # another enabled host's row
    ("rocky", None, False),       # registry unknown → fail closed
    ("gone", BOTH, True),         # stamp left the registry → orphan, primary adopts
])
def test_owns_record_by_stamp_is_the_stamp_rule(primary, host, enabled, expected):
    assert primary._owns_record_by_stamp(host, enabled) is expected


def test_owns_record_still_prefers_a_live_driver(primary):
    """The live-driver clause stays in _owns_record and ONLY there."""
    primary._drivers["s1"] = object()
    rec = _record("s1", "rocky", "idle")
    assert primary._owns_record(rec, BOTH) is True
    assert primary._owns_record_by_stamp("rocky", BOTH) is False


@pytest.fixture
def claiming(primary, monkeypatch):
    """A primary whose lease acquire is recorded instead of hitting the DB, so the
    fence is tested in isolation from the protocol (test_session_lease covers that)."""
    calls: list[str] = []

    async def fake_acquire(session_id, holder, *, ttl_s):
        calls.append(session_id)
        return 1

    monkeypatch.setattr(session_lease, "acquire", fake_acquire)
    primary.acquired = calls
    return primary


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [BOTH, None])
async def test_claim_refuses_a_row_another_host_owns_before_touching_the_lease(
        claiming, caplog, enabled):
    claiming._enabled_host_names.return_value = enabled
    claiming._records["s1"] = _record("s1", "rocky", "idle")
    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        assert await claiming._claim_session("s1", why="resume") is False
    assert claiming.acquired == []                 # never reached acquire()
    assert "s1" not in claiming._lease_fences()
    denied = [r for r in caplog.records if getattr(r, "event", None) == "session.lease_denied"]
    assert denied and denied[0].fields["reason"] == "foreign_stamp:resume"
    assert denied[0].fields["holder"] == "rocky"


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["dobby", None, "gone"])
async def test_claim_proceeds_for_rows_the_primary_owns(claiming, host):
    claiming._records["s1"] = _record("s1", host, "idle")
    assert await claiming._claim_session("s1", why="resume") is True
    assert claiming.acquired == ["s1"]


@pytest.mark.asyncio
async def test_claim_without_a_record_is_decided_by_the_lease_alone(claiming):
    """A fresh spawn has no record yet (D3); the lease protocol decides as before."""
    assert await claiming._claim_session("new", why="spawn") is True
    assert claiming.acquired == ["new"]


@pytest.mark.asyncio
async def test_adopt_bypasses_the_stamp_fence(claiming):
    claiming._records["s1"] = _record("s1", "rocky", "idle")
    assert await claiming._claim_session("s1", why="adopt", adopt=True) is True
    assert claiming.acquired == ["s1"]


@pytest.mark.asyncio
async def test_worker_claims_only_its_own_stamp(claiming, monkeypatch):
    import server.sessions.orchestrator as mod
    monkeypatch.setattr(mod, "daemon_role", lambda: "worker")
    monkeypatch.setattr(mod, "self_host_name", lambda: "rocky")
    claiming._records["mine"] = _record("mine", "rocky", "idle")
    claiming._records["primary"] = _record("primary", "dobby", "idle")
    claiming._records["legacy"] = _record("legacy", None, "idle")
    assert await claiming._claim_session("mine", why="resume") is True
    assert await claiming._claim_session("primary", why="resume") is False
    assert await claiming._claim_session("legacy", why="resume") is False
    assert claiming.acquired == ["mine"]


@pytest.mark.asyncio
async def test_enforcement_off_claims_a_foreign_row_and_warns(claiming, monkeypatch, caplog):
    import server.sessions.orchestrator as mod
    monkeypatch.setattr(mod, "session_lease_enforced", lambda: False)
    claiming._records["s1"] = _record("s1", "rocky", "idle")
    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        assert await claiming._claim_session("s1", why="resume") is True
    assert claiming.acquired == ["s1"]
    assert any("enforcement is OFF" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Refusal logging: one line per distinct fact, not per attempt (#1778)
# ---------------------------------------------------------------------------


@pytest.fixture
def refusing(primary, monkeypatch):
    """A primary whose lease is always held by rocky, so every claim is refused."""
    class _State:
        holder = "rocky"
        fence = 1
        age_s = 3.0

    async def no_fence(session_id, holder, *, ttl_s):
        return None

    async def current(session_id):
        return _State()

    monkeypatch.setattr(session_lease, "acquire", no_fence)
    monkeypatch.setattr(session_lease, "current", current)
    primary.lease_state = _State
    return primary


def _denials(caplog):
    return [r for r in caplog.records
            if getattr(r, "event", None) == "session.lease_denied"]


@pytest.mark.asyncio
async def test_a_repeated_refusal_is_logged_once(refusing, caplog):
    """The retry loops re-enter on a cadence; one line per attempt is what filed
    an auto-error issue with 199 occurrences for a single stuck session (#1778)."""
    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        for _ in range(5):
            assert await refusing._claim_session("s1", why="resume") is False
    assert len(_denials(caplog)) == 1


@pytest.mark.asyncio
async def test_a_new_holder_is_logged_again(refusing, caplog):
    """De-duplication must never hide a CHANGE — a different holder is new news."""
    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        await refusing._claim_session("s1", why="resume")
        refusing.lease_state.holder = "vega"
        await refusing._claim_session("s1", why="resume")
    assert [r.fields["holder"] for r in _denials(caplog)] == ["rocky", "vega"]


@pytest.mark.asyncio
async def test_both_layers_are_distinct_news(refusing, caplog):
    """Layer 1 and the lease answer different questions, so a refusal that moves
    from one to the other is a fact worth a line of its own."""
    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        await refusing._claim_session("s1", why="resume")          # lease
        refusing._records["s1"] = _record("s1", "rocky", "idle")
        await refusing._claim_session("s1", why="resume")          # foreign stamp
    assert [r.fields["reason"] for r in _denials(caplog)] == [
        "resume", "foreign_stamp:resume"]


@pytest.mark.asyncio
async def test_refusals_are_deduped_per_session(refusing, caplog):
    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        await refusing._claim_session("s1", why="resume")
        await refusing._claim_session("s2", why="resume")
        await refusing._claim_session("s1", why="resume")
    assert [r.fields["session_id"] for r in _denials(caplog)] == ["s1", "s2"]


@pytest.mark.asyncio
async def test_a_successful_claim_re_arms_the_report(refusing, caplog, monkeypatch):
    """A claim that wins means the standoff is over; a LATER refusal is news again."""
    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        await refusing._claim_session("s1", why="resume")

        async def with_fence(session_id, holder, *, ttl_s):
            return 7

        monkeypatch.setattr(session_lease, "acquire", with_fence)
        assert await refusing._claim_session("s1", why="resume") is True

        async def no_fence(session_id, holder, *, ttl_s):
            return None

        monkeypatch.setattr(session_lease, "acquire", no_fence)
        await refusing._claim_session("s1", why="resume")
    assert len(_denials(caplog)) == 2


@pytest.mark.asyncio
async def test_a_lease_refusal_never_reaches_warning(refusing, caplog):
    """A live twin holding the lease is the fence doing its job (#1379), not a fault:
    every WARNING auto-files an issue, so the refusal must journal at INFO (#1785)."""
    with caplog.at_level(logging.DEBUG, logger="server.sessions.orchestrator"):
        for _ in range(3):
            assert await refusing._claim_session("s1", why="resume") is False
    refusals = [r for r in caplog.records if "refusing to resume" in r.getMessage()]
    assert refusals, "the refusal must still be journaled"
    assert [r for r in refusals if r.levelno >= logging.WARNING] == []
    assert [r.levelno for r in _denials(caplog)] == [logging.INFO]


@pytest.fixture
def ticking(claiming, monkeypatch):
    """A primary whose renew/release are recorded; the clock is injectable."""
    renewed: list[str] = []
    released: list[str] = []

    async def fake_renew(session_id, holder, fence):
        renewed.append(session_id)
        return True

    async def fake_release(session_id, holder, fence):
        released.append(session_id)

    monkeypatch.setattr(session_lease, "renew", fake_renew)
    monkeypatch.setattr(session_lease, "release", fake_release)
    claiming.renewed, claiming.released = renewed, released
    claiming._now = 1000.0
    claiming._clock = lambda: claiming._now
    return claiming


@pytest.mark.asyncio
async def test_a_fresh_claim_is_renewed_not_released_while_the_driver_builds(ticking):
    assert await ticking._claim_session("s1", why="resume") is True
    ticking._now += 1.0                       # the tick at t+1 that killed #1826
    await ticking._session_lease_tick()
    assert ticking.released == []
    assert ticking.renewed == ["s1"]
    assert "s1" in ticking._lease_fences()


@pytest.mark.asyncio
async def test_registration_clears_the_build_marker(ticking):
    await ticking._claim_session("s1", why="resume")
    ticking._drivers["s1"] = object()
    await ticking._session_lease_tick()
    assert "s1" not in ticking._lease_building()
    assert ticking.renewed == ["s1"] and ticking.released == []


@pytest.mark.asyncio
async def test_a_build_that_never_registers_is_released_after_spawn_wait(ticking):
    await ticking._claim_session("s1", why="resume")
    ticking._now += SPAWN_WAIT_S + 1
    await ticking._session_lease_tick()
    assert ticking.released == ["s1"]
    assert "s1" not in ticking._lease_fences()
    assert "s1" not in ticking._lease_building()


@pytest.mark.asyncio
async def test_teardown_drops_the_build_marker_too(ticking):
    await ticking._claim_session("s1", why="resume")
    await ticking._release_session_lease("s1")
    assert "s1" not in ticking._lease_building()
    assert ticking.released == ["s1"]


class LiveDriver:
    def __init__(self, turn_active=False, drain_alive=False):
        self.turn_active = turn_active
        self.drain_alive = drain_alive
        self.closed = False

    async def aclose(self):
        self.closed = True


@pytest.fixture
def reconciling(ticking, monkeypatch):
    """`ticking` plus the teardown seams the reconcile calls."""
    torn: list[str] = []

    async def fake_teardown(sid):
        torn.append(sid)
        ticking._lease_fences().pop(sid, None)
        ticking._lease_building().pop(sid, None)
        return ticking._drivers.pop(sid, None)

    async def fake_close(driver, sid):
        driver.closed = True

    ticking._teardown_driver_state = fake_teardown
    ticking._close_automation_driver = fake_close
    ticking._spawn = lambda coro, name=None: asyncio.ensure_future(coro)
    ticking.torn = torn
    return ticking


@pytest.mark.asyncio
async def test_tick_hands_back_a_lease_held_for_a_row_another_host_owns(reconciling, caplog):
    """The automated form of the 2026-09-16 manual repair: dobby holds and drives a
    rocky-stamped row; one tick closes the driver and releases the lease."""
    await reconciling._claim_session("s1", why="adopt", adopt=True)
    reconciling._records["s1"] = _record("s1", "rocky", "idle")
    driver = LiveDriver()
    reconciling._drivers["s1"] = driver
    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        await reconciling._session_lease_tick()
    await asyncio.sleep(0)                        # let the deferred close run
    assert reconciling.torn == ["s1"]
    assert "s1" not in reconciling._drivers and driver.closed
    assert reconciling.renewed == []              # released, not renewed, this pass
    assert "s1" in reconciling._records           # the fleet mirror survives
    ev = [r for r in caplog.records if getattr(r, "event", None) == "session.lease_reconciled"]
    assert ev and ev[0].fields["session_id"] == "s1" and ev[0].fields["host"] == "rocky"


@pytest.mark.asyncio
async def test_tick_defers_while_a_turn_is_active(reconciling, caplog):
    await reconciling._claim_session("s1", why="adopt", adopt=True)
    reconciling._records["s1"] = _record("s1", "rocky", "idle")
    reconciling._drivers["s1"] = LiveDriver(turn_active=True)
    with caplog.at_level(logging.CRITICAL, logger="server.sessions.orchestrator"):
        await reconciling._session_lease_tick()
    assert reconciling.torn == [] and reconciling.renewed == ["s1"]
    ev = [r for r in caplog.records if getattr(r, "event", None) == "session.lease_foreign_holder"]
    assert ev and ev[0].fields["action"] == "deferred_turn_active"


@pytest.mark.asyncio
async def test_tick_never_reconciles_when_the_registry_is_unknown(reconciling):
    reconciling._enabled_host_names.return_value = None
    await reconciling._claim_session("s1", why="adopt", adopt=True)
    reconciling._records["s1"] = _record("s1", "rocky", "idle")
    reconciling._drivers["s1"] = LiveDriver()
    await reconciling._session_lease_tick()
    assert reconciling.torn == [] and reconciling.renewed == ["s1"]


@pytest.mark.asyncio
async def test_tick_logs_only_when_enforcement_is_off(reconciling, monkeypatch, caplog):
    import server.sessions.orchestrator as mod
    monkeypatch.setattr(mod, "session_lease_enforced", lambda: False)
    await reconciling._claim_session("s1", why="adopt", adopt=True)
    reconciling._records["s1"] = _record("s1", "rocky", "idle")
    reconciling._drivers["s1"] = LiveDriver()
    with caplog.at_level(logging.CRITICAL, logger="server.sessions.orchestrator"):
        await reconciling._session_lease_tick()
    assert reconciling.torn == [] and reconciling.renewed == ["s1"]
    ev = [r for r in caplog.records if getattr(r, "event", None) == "session.lease_foreign_holder"]
    assert ev and ev[0].fields["action"] == "log_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["dobby", None, "gone"])
async def test_tick_leaves_owned_rows_alone(reconciling, host):
    await reconciling._claim_session("s1", why="spawn")
    reconciling._records["s1"] = _record("s1", host, "idle")
    reconciling._drivers["s1"] = LiveDriver()
    await reconciling._session_lease_tick()
    assert reconciling.torn == [] and reconciling.renewed == ["s1"]


@pytest.mark.asyncio
async def test_tick_ignores_a_live_driver_with_no_record(reconciling):
    """test_session_lease's harness registers bare object() drivers with no
    record; the reconcile must not touch them."""
    await reconciling._claim_session("s1", why="spawn")
    reconciling._drivers["s1"] = object()
    await reconciling._session_lease_tick()
    assert reconciling.torn == [] and reconciling.renewed == ["s1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["_swapping", "_spawning"])
async def test_tick_defers_while_a_swap_or_spawn_is_in_flight(reconciling, caplog, marker):
    """A mid-swap teardown would leave the NEW driver live with no lease and no
    fence — a state the tick can never see again. Defer instead."""
    await reconciling._claim_session("s1", why="adopt", adopt=True)
    reconciling._records["s1"] = _record("s1", "rocky", "idle")
    reconciling._drivers["s1"] = LiveDriver()
    setattr(reconciling, marker, {"s1": asyncio.get_running_loop().create_future()})
    with caplog.at_level(logging.CRITICAL, logger="server.sessions.orchestrator"):
        await reconciling._session_lease_tick()
    assert reconciling.torn == [] and reconciling.renewed == ["s1"]
    ev = [r for r in caplog.records if getattr(r, "event", None) == "session.lease_foreign_holder"]
    assert ev and ev[0].fields["action"] == "deferred_build_in_flight"


@pytest.mark.asyncio
async def test_a_repeated_deferral_demotes_to_debug(reconciling, caplog):
    """A wedged turn_active driver ticks every ~24s forever while it cannot act
    (#1832): the first tick is a real CRITICAL alarm, but every later tick of
    the SAME condition must not re-file it — only a heartbeat at DEBUG."""
    await reconciling._claim_session("s1", why="adopt", adopt=True)
    reconciling._records["s1"] = _record("s1", "rocky", "idle")
    reconciling._drivers["s1"] = LiveDriver(turn_active=True)
    with caplog.at_level(logging.DEBUG, logger="server.sessions.orchestrator"):
        await reconciling._session_lease_tick()
        await reconciling._session_lease_tick()
    ev = [r for r in caplog.records if getattr(r, "event", None) == "session.lease_foreign_holder"]
    assert [r.levelno for r in ev] == [logging.CRITICAL, logging.DEBUG]
    assert [r.fields["action"] for r in ev] == ["deferred_turn_active"] * 2


@pytest.mark.asyncio
async def test_tick_isolates_a_failing_reconcile(reconciling, caplog):
    """A raising teardown for one session must not skip the renew of the others,
    and the failing session itself is still renewed (lease kept, retried next tick)."""
    await reconciling._claim_session("bad", why="adopt", adopt=True)
    await reconciling._claim_session("good", why="spawn")
    reconciling._records["bad"] = _record("bad", "rocky", "idle")
    reconciling._records["good"] = _record("good", "dobby", "idle")
    reconciling._drivers["bad"] = LiveDriver()
    reconciling._drivers["good"] = LiveDriver()

    async def boom(sid):
        raise RuntimeError("display teardown wedged")

    reconciling._teardown_driver_state = boom
    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        await reconciling._session_lease_tick()
    assert sorted(reconciling.renewed) == ["bad", "good"]
    assert reconciling.released == []
    ev = [r for r in caplog.records if getattr(r, "event", None) == "session.lease_reconcile_failed"]
    assert ev and ev[0].fields["session_id"] == "bad"
