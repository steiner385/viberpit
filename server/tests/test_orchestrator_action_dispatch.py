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
    # cicd-doctor is the third registered provider (#465).
    assert orch._action_providers["cicd-doctor"].valid_kinds == frozenset({"file_issue"})


@pytest.mark.asyncio
async def test_actionable_unknown_provider_returns_empty():
    orch = _orch()
    # autopilot is a real automation with no action provider registered.
    assert await orch.automation_actionable("autopilot") == []


@pytest.mark.asyncio
async def test_actionable_pr_doctor_empty_cache_is_empty_list():
    orch = _orch()
    # No PRs in cache → empty, but dispatch path must run without error.
    assert await orch.automation_actionable("pr-doctor") == []
