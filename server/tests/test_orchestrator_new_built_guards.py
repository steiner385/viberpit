"""Bookkeeping added for #947 (holds, dirty status, delete state) must tolerate an
orchestrator built with ``__new__`` and no ``__init__`` — the fixture shape of
``test_session_move.py`` / ``test_branch_deletion.py`` — the way ``_queue_drain_lock``
does (#662). ``_retarget_record`` raised AttributeError there (28 failures)."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from server.sessions.orchestrator import SessionOrchestrator


def _bare() -> SessionOrchestrator:
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    orch._worktrees = {}
    orch._pending_prompt_preambles = {}
    orch._resume_recovery = {}
    # Pre-#947 attributes the delete-state readers also touch.
    orch._archive_purge_poisoned = set()
    orch._archive_purge_strikes = {}
    return orch


def test_retarget_record_on_a_bare_orchestrator_does_not_raise():
    orch = _bare()
    with patch("server.sessions.orchestrator.daemon_role", return_value="worker"):
        orch._retarget_record("s1", "other-host")
    assert orch._hold_kinds() == {}


def test_lazy_accessors_create_then_reuse_the_attribute():
    orch = _bare()
    orch._hold_kinds()["s1"] = "permission"
    assert orch._open_hold_kind == {"s1": "permission"}
    orch._dirty_ids().add("s2")
    assert orch._dirty_status == {"s2"}
    orch._pending_delete_errors()["s3"] = "ESTALE"
    assert orch._pending_deletes == {"s3": "ESTALE"}
    assert orch.delete_state("s3") == "pending" and orch.delete_state("zz") is None
    assert orch.pending_delete_states() == {"s3": "pending"}


def test_resolve_hold_on_terminal_on_a_bare_orchestrator_returns_quietly():
    orch = _bare()
    asyncio.run(orch._resolve_hold_on_terminal("s1", None))
