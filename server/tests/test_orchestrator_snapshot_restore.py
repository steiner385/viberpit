import logging

import pytest
from server.sessions import deploy_snapshot as ds


class FakeRec:
    def __init__(self, status, host=None, id=None):
        self.status = status
        self.host = host        # fleet stamp, read by the real _owns_record
        self.id = id


class FakeDriver:
    def __init__(self): self.prompts = []

    async def send_prompt(self, text, images=None): self.prompts.append(text)


class MiniOrch:
    """Exercises the snapshot/restore logic with the real functions bound in."""
    from server.sessions.orchestrator import SessionOrchestrator
    snapshot_deploy = SessionOrchestrator.snapshot_deploy
    restore_deploy = SessionOrchestrator.restore_deploy
    _owns_record = SessionOrchestrator._owns_record

    def __init__(self, drivers, records, resumable, last_prompts=None, enabled_hosts=None):
        self._drivers = drivers
        self._records = records
        self._resumable = resumable        # set of ids ensure_driver succeeds for
        self._resume_recovery: dict[str, int] = {}   # restore_deploy enrolls failures here
        self._last_prompt: dict[str, str] = dict(last_prompts or {})
        self._enabled_hosts = enabled_hosts          # what _enabled_host_names answers
        self.ensured: list[str] = []
        self.built: dict[str, FakeDriver] = {}       # drivers handed out by ensure_driver

    async def _enabled_host_names(self):
        return self._enabled_hosts

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


@pytest.fixture
def primary_dobby(monkeypatch):
    import server.sessions.orchestrator as mod
    monkeypatch.setattr(mod, "daemon_role", lambda: "primary")
    monkeypatch.setattr(mod, "self_host_name", lambda: "dobby")


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [frozenset({"dobby", "rocky"}), None])
async def test_restore_leaves_foreign_host_rows_to_their_owner(
        tmp_path, caplog, primary_dobby, enabled):
    """The snapshot lists the drivers this process HELD, not the rows it OWNS.

    A worker's row that a pre-fence automation path once grabbed on the primary
    would otherwise be resumed here again on EVERY deploy — outside its worktree,
    in a bare mkdir'd cwd — and its lease would fence the owning host out of its
    own session for as long as the primary keeps restarting (#1824). Leave it to
    its owner, log it, and do not enroll it for retry. An unknown host registry
    fails closed, exactly like the other local actors (#1738)."""
    p = str(tmp_path / "live.json")
    ds.write_snapshot([
        {"id": "mine", "status": "idle"},
        {"id": "foreign", "status": "in_progress", "lastPrompt": "go"},
    ], path=p)
    o = MiniOrch(drivers={},
                 records={"mine": FakeRec("idle", host="dobby", id="mine"),
                          "foreign": FakeRec("idle", host="rocky", id="foreign")},
                 resumable={"mine", "foreign"}, enabled_hosts=enabled)
    with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
        res = await o.restore_deploy(path=p)
    assert o.ensured == ["mine"]
    assert res["restored"] == ["mine"]
    assert res["skipped"] == ["foreign"]
    assert res["failed"] == [] and res["replayed"] == []
    assert "foreign" not in o._resume_recovery      # the owner resumes it, not us
    assert "foreign" not in o.built                 # no driver → no replayed prompt
    skips = [r for r in caplog.records
             if getattr(r, "event", None) == "deploy.restore.skip_foreign"]
    assert [r.fields["session_id"] for r in skips] == ["foreign"]
    assert skips[0].fields["host"] == "rocky"
    outcome = [r for r in caplog.records if getattr(r, "event", None) == "deploy.restore"]
    assert outcome[0].fields["skipped"] == ["foreign"]


@pytest.mark.asyncio
async def test_restore_still_resumes_unstamped_and_orphaned_rows(tmp_path, primary_dobby):
    """Ownership keeps its existing policy: the primary owns NULL-host rows and rows
    whose host has left the registry (orphan adoption); a row with no in-memory
    record at all is not fenced (ensure_driver decides, as before)."""
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "legacy", "status": "idle"},
                       {"id": "orphan", "status": "idle"},
                       {"id": "unknown", "status": "idle"}], path=p)
    o = MiniOrch(drivers={},
                 records={"legacy": FakeRec("idle", host=None, id="legacy"),
                          "orphan": FakeRec("idle", host="gone", id="orphan")},
                 resumable={"legacy", "orphan"},
                 enabled_hosts=frozenset({"dobby", "rocky"}))
    res = await o.restore_deploy(path=p)
    assert o.ensured == ["legacy", "orphan", "unknown"]
    assert res["restored"] == ["legacy", "orphan"]
    assert res["failed"] == ["unknown"] and res["skipped"] == []


@pytest.mark.asyncio
async def test_restore_on_worker_resumes_only_its_own_stamp(tmp_path, monkeypatch):
    import server.sessions.orchestrator as mod
    monkeypatch.setattr(mod, "daemon_role", lambda: "worker")
    monkeypatch.setattr(mod, "self_host_name", lambda: "rocky")
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "mine", "status": "idle"},
                       {"id": "primary", "status": "idle"},
                       {"id": "legacy", "status": "idle"}], path=p)
    o = MiniOrch(drivers={},
                 records={"mine": FakeRec("idle", host="rocky", id="mine"),
                          "primary": FakeRec("idle", host="dobby", id="primary"),
                          "legacy": FakeRec("idle", host=None, id="legacy")},
                 resumable={"mine", "primary", "legacy"},
                 enabled_hosts=frozenset({"dobby", "rocky"}))
    res = await o.restore_deploy(path=p)
    assert o.ensured == ["mine"]
    assert res["skipped"] == ["primary", "legacy"]


def test_snapshot_restore_registered_as_rpc():
    import inspect
    from server.ipc import orchestrator_rpc
    src = inspect.getsource(orchestrator_rpc.register_orchestrator_rpc)
    assert "snapshot_deploy" in src and "restore_deploy" in src
