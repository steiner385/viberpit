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
    _restore_deploy_once = SessionOrchestrator._restore_deploy_once
    _deploy_restore = None
    _owns_record = SessionOrchestrator._owns_record
    _owns_record_by_stamp = SessionOrchestrator._owns_record_by_stamp
    orphaned_interaction = SessionOrchestrator.orphaned_interaction
    _replayed_ids = SessionOrchestrator._replayed_ids

    def __init__(self, drivers, records, resumable, last_prompts=None, enabled_hosts=None,
                 archived=None):
        self._drivers = drivers
        self._records = records
        self._resumable = resumable        # set of ids ensure_driver succeeds for
        self._resume_recovery: dict[str, int] = {}   # restore_deploy enrolls failures here
        self._last_prompt: dict[str, str] = dict(last_prompts or {})
        self._enabled_hosts = enabled_hosts          # what _enabled_host_names answers
        self._archived = set(archived or ())         # ids with an archived/ended row (#1035)
        self.ensured: list[str] = []
        self.built: dict[str, FakeDriver] = {}       # drivers handed out by ensure_driver

    async def _enabled_host_names(self):
        return self._enabled_hosts

    async def _session_row_liveness(self, ids):
        inactive = {i for i in ids if i in self._archived}
        return set(ids) - inactive, inactive

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


def test_snapshot_excludes_archived_and_ended_drivers(tmp_path):
    """#1035: a driver whose in-memory record already reads archived/ended (e.g. a
    stale mirror driver on another host, or a record archived just before the
    snapshot ran) must not be captured — snapshotting it only guarantees a doomed
    restore-and-retry cycle after the restart."""
    p = str(tmp_path / "live.json")
    o = MiniOrch(drivers={"s1": 1, "s2": 1, "s3": 1},
                 records={"s1": FakeRec("working"), "s2": FakeRec("archived"),
                          "s3": FakeRec("ended")},
                 resumable=set())
    snap = o.snapshot_deploy(path=p)
    ids = {e["id"] for e in snap["live"]}
    assert ids == {"s1"}


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


@pytest.mark.asyncio
async def test_restore_skips_already_archived_row(tmp_path):
    """#1035: a snapshotted id whose DB row is already archived/ended (e.g. the
    user archived it after the snapshot was taken but before the restart landed)
    must not be retried or enrolled for recovery — both banner actions ("open",
    "archive") are impossible for it, so it is reported skipped instead."""
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "gone", "status": "idle"},
                       {"id": "s1", "status": "idle"}], path=p)
    o = MiniOrch(drivers={}, records={}, resumable={"gone", "s1"}, archived={"gone"})
    res = await o.restore_deploy(path=p)
    assert o.ensured == ["s1"]
    assert res["restored"] == ["s1"]
    assert res["skipped"] == ["gone"]
    assert "gone" not in o._resume_recovery


def test_snapshot_restore_registered_as_rpc():
    import inspect
    from server.ipc import orchestrator_rpc
    src = inspect.getsource(orchestrator_rpc.register_orchestrator_rpc)
    assert "snapshot_deploy" in src and "restore_deploy" in src


# --- single-flight restore (#3082) ----------------------------------------------------
# The controller retries a timed-out restore_deploy. A timeout is outcome-unknown, so a
# retry must join the run in flight or read its result, never re-launch or re-replay.

class SlowOrch(MiniOrch):
    """ensure_driver parks on a gate, like a real resume that outlives the RPC timeout."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        import asyncio
        self.gate = asyncio.Event()

    async def ensure_driver(self, session_id, identity="system"):
        await self.gate.wait()
        return await super().ensure_driver(session_id, identity)


@pytest.mark.asyncio
async def test_retry_while_in_flight_joins_the_same_run(tmp_path):
    import asyncio
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "s1", "status": "in_progress", "lastPrompt": "go on"}], path=p)
    o = SlowOrch(drivers={}, records={}, resumable={"s1"})
    first = asyncio.ensure_future(o.restore_deploy(path=p))
    await asyncio.sleep(0)
    first.cancel()                       # the controller's call timed out and went away
    with pytest.raises(asyncio.CancelledError):
        await first
    retry = asyncio.ensure_future(o.restore_deploy(path=p))
    await asyncio.sleep(0)
    o.gate.set()
    res = await retry
    assert res["restored"] == ["s1"] and res["replayed"] == ["s1"]
    assert o.ensured == ["s1"], "the abandoned call's run is the one that finished"
    assert o.built["s1"].prompts == ["go on"], "replayed exactly once"


@pytest.mark.asyncio
async def test_retry_after_finish_returns_the_cached_result(tmp_path):
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "s1", "status": "idle"}, {"id": "s2", "status": "idle"}], path=p)
    o = MiniOrch(drivers={}, records={}, resumable={"s1"})
    first = await o.restore_deploy(path=p)
    again = await o.restore_deploy(path=p)
    assert again == first == {"restored": ["s1"], "failed": ["s2"], "replayed": [],
                              "skipped": []}
    assert o.ensured == ["s1", "s2"], "the snapshot is not re-read: no second run"


@pytest.mark.asyncio
async def test_a_new_snapshot_starts_a_new_restore(tmp_path):
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "s1", "status": "idle"}], path=p)
    o = MiniOrch(drivers={}, records={}, resumable={"s1", "s2"})
    await o.restore_deploy(path=p)
    o._drivers = {"s2": 1}
    o._records = {"s2": FakeRec("idle")}
    o.snapshot_deploy(path=p)
    res = await o.restore_deploy(path=p)
    assert res["restored"] == ["s2"]


@pytest.mark.asyncio
async def test_a_run_that_raised_is_not_cached(tmp_path):
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "s1", "status": "idle"}], path=p)
    o = MiniOrch(drivers={}, records={}, resumable={"s1"})
    calls = {"n": 0}
    real = o._session_row_liveness

    async def flaky(ids):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db blip")
        return await real(ids)

    o._session_row_liveness = flaky
    with pytest.raises(RuntimeError):
        await o.restore_deploy(path=p)
    res = await o.restore_deploy(path=p)
    assert res["restored"] == ["s1"]
