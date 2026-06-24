import pytest
from server.sessions import deploy_snapshot as ds


class FakeRec:
    def __init__(self, status): self.status = status


class MiniOrch:
    """Exercises the snapshot/restore logic with the real functions bound in."""
    from server.sessions.orchestrator import SessionOrchestrator
    snapshot_deploy = SessionOrchestrator.snapshot_deploy
    restore_deploy = SessionOrchestrator.restore_deploy

    def __init__(self, drivers, records, resumable):
        self._drivers = drivers
        self._records = records
        self._resumable = resumable        # set of ids ensure_driver succeeds for
        self.ensured: list[str] = []

    async def ensure_driver(self, session_id, identity="system"):
        self.ensured.append(session_id)
        return object() if session_id in self._resumable else None


def test_snapshot_captures_live_drivers(tmp_path):
    p = str(tmp_path / "live.json")
    o = MiniOrch(drivers={"s1": 1, "s2": 1},
                 records={"s1": FakeRec("working"), "s2": FakeRec("waiting_input")},
                 resumable=set())
    snap = o.snapshot_deploy(path=p)
    ids = {e["id"] for e in snap["live"]}
    assert ids == {"s1", "s2"}
    assert ds.read_snapshot(path=p) == snap["live"]


@pytest.mark.asyncio
async def test_restore_relaunches_and_splits_results(tmp_path):
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "s1", "status": "working"}, {"id": "s2", "status": "idle"}], path=p)
    o = MiniOrch(drivers={}, records={}, resumable={"s1"})
    res = await o.restore_deploy(path=p)
    assert res["restored"] == ["s1"] and res["failed"] == ["s2"]
    assert set(o.ensured) == {"s1", "s2"}
    assert ds.read_snapshot(path=p) == []   # cleared
