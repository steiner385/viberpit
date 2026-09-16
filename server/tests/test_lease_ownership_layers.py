"""Session lease ownership: choke-point fence, build-window grace, self-healing
reconcile (spec docs/superpowers/specs/2026-09-16-session-lease-ownership-design.md).

Companion to test_session_lease.py (the lease protocol and gate) and
test_recovery_actor_ownership.py (per-caller fences). These pin the three layers
that make the 331f7d98 split-brain (#1824, #1826) structurally impossible."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from server.sessions import session_lease
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.test_host_partitioned_ownership import _record

BOTH = frozenset({"dobby", "rocky"})


@pytest.fixture
def primary(monkeypatch):
    import server.sessions.orchestrator as mod
    monkeypatch.setattr(mod, "daemon_role", lambda: "primary")
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "dobby")
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
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "rocky")
    claiming._records["mine"] = _record("mine", "rocky", "idle")
    claiming._records["primary"] = _record("primary", "dobby", "idle")
    claiming._records["legacy"] = _record("legacy", None, "idle")
    assert await claiming._claim_session("mine", why="resume") is True
    assert await claiming._claim_session("primary", why="resume") is False
    assert await claiming._claim_session("legacy", why="resume") is False
    assert claiming.acquired == ["mine"]


@pytest.mark.asyncio
async def test_enforcement_off_claims_a_foreign_row_and_warns(claiming, monkeypatch, caplog):
    monkeypatch.setenv("CCSWITCH_SESSION_LEASE_ENFORCE", "false")
    claiming._records["s1"] = _record("s1", "rocky", "idle")
    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        assert await claiming._claim_session("s1", why="resume") is True
    assert claiming.acquired == ["s1"]
    assert any("enforcement is OFF" in r.getMessage() for r in caplog.records)
