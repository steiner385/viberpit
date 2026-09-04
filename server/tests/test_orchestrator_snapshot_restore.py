import logging

import pytest
from server.sessions import deploy_snapshot as ds


class FakeRec:
    def __init__(self, status): self.status = status


class FakeDriver:
    def __init__(self): self.prompts = []

    async def send_prompt(self, text, images=None): self.prompts.append(text)


class MiniOrch:
    """Exercises the snapshot/restore logic with the real functions bound in."""
    from server.sessions.orchestrator import SessionOrchestrator
    snapshot_deploy = SessionOrchestrator.snapshot_deploy
    restore_deploy = SessionOrchestrator.restore_deploy

    def __init__(self, drivers, records, resumable, last_prompts=None):
        self._drivers = drivers
        self._records = records
        self._resumable = resumable        # set of ids ensure_driver succeeds for
        self._resume_recovery: dict[str, int] = {}   # restore_deploy enrolls failures here
        self._last_prompt: dict[str, str] = dict(last_prompts or {})
        self.ensured: list[str] = []
        self.built: dict[str, FakeDriver] = {}       # drivers handed out by ensure_driver

    async def ensure_driver(self, session_id, identity="system"):
        self.ensured.append(session_id)
        if session_id not in self._resumable:
            return None
        return self.built.setdefault(session_id, FakeDriver())


def test_snapshot_captures_live_drivers(tmp_path):
    p = str(tmp_path / "live.json")
    o = MiniOrch(drivers={"s1": 1, "s2": 1},
                 records={"s1": FakeRec("working"), "s2": FakeRec("waiting_input")},
                 resumable=set())
    snap = o.snapshot_deploy(path=p)
    ids = {e["id"] for e in snap["live"]}
    assert ids == {"s1", "s2"}
    assert ds.read_snapshot(path=p) == snap["live"]


def test_snapshot_records_last_prompt_for_mid_turn_sessions(tmp_path):
    """A mid-turn session's interrupted prompt is captured so the post-restart
    daemon (whose _last_prompt dict starts empty) can re-issue it. Sessions
    paused on the user (waiting_input) are NOT replayable — re-driving the turn
    would repeat work the user was asked to approve."""
    p = str(tmp_path / "live.json")
    o = MiniOrch(drivers={"s1": 1, "s2": 1, "s3": 1},
                 records={"s1": FakeRec("in_progress"), "s2": FakeRec("waiting_input"),
                          "s3": FakeRec("in_progress")},
                 resumable=set(),
                 last_prompts={"s1": "finish the refactor", "s2": "answer me"})
    snap = o.snapshot_deploy(path=p)
    by_id = {e["id"]: e for e in snap["live"]}
    assert by_id["s1"]["lastPrompt"] == "finish the refactor"
    assert "lastPrompt" not in by_id["s2"]   # awaiting the user, not replayable
    assert "lastPrompt" not in by_id["s3"]   # mid-turn but no prompt recorded


@pytest.mark.asyncio
async def test_restore_relaunches_and_splits_results(tmp_path):
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "s1", "status": "working"}, {"id": "s2", "status": "idle"}], path=p)
    o = MiniOrch(drivers={}, records={}, resumable={"s1"})
    res = await o.restore_deploy(path=p)
    assert res["restored"] == ["s1"] and res["failed"] == ["s2"]
    assert set(o.ensured) == {"s1", "s2"}
    assert ds.read_snapshot(path=p) == []   # cleared


@pytest.mark.asyncio
async def test_restore_replays_interrupted_turn(tmp_path):
    """A snapshotted mid-turn prompt is re-issued on the resumed driver, and the
    fresh daemon's _last_prompt is seeded so later 429/401 replays still work."""
    p = str(tmp_path / "live.json")
    ds.write_snapshot([
        {"id": "s1", "status": "in_progress", "lastPrompt": "finish the refactor"},
        {"id": "s2", "status": "idle"},
    ], path=p)
    o = MiniOrch(drivers={}, records={}, resumable={"s1", "s2"})
    res = await o.restore_deploy(path=p)
    assert res["restored"] == ["s1", "s2"]
    assert res["replayed"] == ["s1"]
    assert o.built["s1"].prompts == ["finish the refactor"]
    assert o.built["s2"].prompts == []
    assert o._last_prompt["s1"] == "finish the refactor"


@pytest.mark.asyncio
async def test_restore_replay_failure_keeps_session_restored(tmp_path):
    """A failed re-send degrades to a logged skip: the session still counts as
    restored (it has a live driver) and the restore itself never raises."""
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "s1", "status": "in_progress", "lastPrompt": "go"}], path=p)
    o = MiniOrch(drivers={}, records={}, resumable={"s1"})

    class BoomDriver(FakeDriver):
        async def send_prompt(self, text, images=None): raise RuntimeError("pty gone")

    o.built["s1"] = BoomDriver()
    res = await o.restore_deploy(path=p)
    assert res["restored"] == ["s1"]
    assert res["replayed"] == []


@pytest.mark.asyncio
async def test_restore_logs_outcome_breadcrumb(tmp_path, caplog):
    """restore_deploy emits a deploy.restore log_event with the full outcome —
    the controller's status object is not durable across its self-restart, so
    the daemon journal must carry the result (issue #864)."""
    p = str(tmp_path / "live.json")
    ds.write_snapshot([
        {"id": "s1", "status": "in_progress", "lastPrompt": "go"},
        {"id": "s2", "status": "idle"},
    ], path=p)
    o = MiniOrch(drivers={}, records={}, resumable={"s1"})
    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        await o.restore_deploy(path=p)
    recs = [r for r in caplog.records if getattr(r, "event", None) == "deploy.restore"]
    assert len(recs) == 1
    assert recs[0].fields["restored"] == ["s1"]
    assert recs[0].fields["failed"] == ["s2"]
    assert recs[0].fields["replayed"] == ["s1"]


def test_snapshot_restore_registered_as_rpc():
    import inspect
    from server.ipc import orchestrator_rpc
    src = inspect.getsource(orchestrator_rpc.register_orchestrator_rpc)
    assert "snapshot_deploy" in src and "restore_deploy" in src
