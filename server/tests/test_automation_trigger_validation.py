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
async def test_trigger_no_provider_returns_structured_status():
    r = await _orch().automation_trigger("cicd-doctor", "x", "y")
    assert r["status"] == "no_provider"
    assert r["level"] == "error"


@pytest.mark.asyncio
async def test_trigger_invalid_kind_returns_structured_status():
    r = await _orch().automation_trigger("pr-doctor", "owner/repo#1", "bogus")
    assert r["status"] == "invalid_kind"
    assert r["level"] == "error"
