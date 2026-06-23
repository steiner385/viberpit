import pytest
from server.db import init_db
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions import compaction_stats as cs
from server.tests.fakes.fake_factory import fake_sdk_factory


@pytest.mark.asyncio
async def test_orchestrator_stats_methods_persist_and_serve(tmp_path):
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    assert await orch._condense_estimate() == cs.DEFAULT_ESTIMATE_MS
    await orch._record_condense_ms(7000)
    assert await orch._condense_estimate() == 7000
