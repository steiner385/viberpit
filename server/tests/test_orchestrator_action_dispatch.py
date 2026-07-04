import os
from unittest.mock import patch

import pytest

from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


def _orch():
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1"}):
        return SessionOrchestrator(sdk_factory=fake_sdk_factory())


@pytest.mark.asyncio
async def test_dispatch_registers_pr_doctor_and_backlog():
    orch = _orch()
    assert orch._action_providers["pr-doctor"].valid_kinds == frozenset(
        {"failing_ci", "merge_conflict", "out_of_date"})
    assert orch._action_providers["backlog-runner"].valid_kinds == frozenset({"implement"})
    assert "cicd-doctor" not in orch._action_providers


@pytest.mark.asyncio
async def test_actionable_unknown_provider_returns_empty():
    orch = _orch()
    assert await orch.automation_actionable("cicd-doctor") == []


@pytest.mark.asyncio
async def test_actionable_pr_doctor_empty_cache_is_empty_list():
    orch = _orch()
    # No PRs in cache → empty, but dispatch path must run without error.
    assert await orch.automation_actionable("pr-doctor") == []
