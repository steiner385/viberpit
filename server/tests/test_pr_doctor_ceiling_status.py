import os
from unittest.mock import patch

import pytest

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.selection import AllAccountsBusy
from server.github.pr_status import PullRequestInfo
from server.tests.fakes.fake_factory import fake_sdk_factory


@pytest.mark.asyncio
async def test_session_fix_returns_ceiling_when_all_accounts_busy():
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1"}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    info = PullRequestInfo(number=11143, url="https://x/11143", state="open",
                           checks="failing", title="red", repo="cairnea/KinDash",
                           head_ref="fix-lint")

    async def _boom(*a, **k):
        raise AllAccountsBusy("all accounts at/above usage ceiling")

    # No live driver → fresh-spawn path; the clone succeeds, the spawn refuses.
    with patch("server.sessions.orchestrator.gitrepos.ensure_clone", return_value="/tmp/x"), \
         patch.object(orch._pr_doctor_ctx, "spawn_session", _boom):
        status, sid = await orch._pr_doctor_session_fix(
            "cairnea/KinDash", "fix-lint", info, "failing_ci", None)
    assert status == "ceiling"
    assert sid is None
