import asyncio
import pytest
from server.sessions.types import SessionRecord


class _FakeDriver:
    def __init__(self): self.model = "untouched"
    async def set_model(self, m): self.model = m


@pytest.mark.asyncio
async def test_set_model_for_session_updates_record_and_driver(orchestrator_with_session):
    orch, sid, driver = orchestrator_with_session
    await orch.set_model_for_session(sid, "opus")
    assert driver.model == "opus"
    assert orch._records[sid].model == "opus"


@pytest.fixture
def orchestrator_with_session():
    # Build a minimal orchestrator instance without starting drivers/db.
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id="s1", name="n", account="a", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    driver = _FakeDriver()
    orch._records = {"s1": rec}
    orch._drivers = {"s1": driver}

    async def _noop_persist(sid, model):  # patched persistence
        rec.model = model
    orch._persist_model = _noop_persist  # type: ignore[attr-defined]
    return orch, "s1", driver
