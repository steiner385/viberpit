"""Session lease ownership: choke-point fence, build-window grace, self-healing
reconcile (spec docs/superpowers/specs/2026-09-16-session-lease-ownership-design.md).

Companion to test_session_lease.py (the lease protocol and gate) and
test_recovery_actor_ownership.py (per-caller fences). These pin the three layers
that make the 331f7d98 split-brain (#1824, #1826) structurally impossible."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

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
