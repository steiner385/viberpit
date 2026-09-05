import pytest
from unittest import mock
from server.sessions.fleet_orchestrator import FleetOrchestrator
from server.sessions.facade import _DEADLINE_ATTR


class _FakeOrch:
    def __init__(self, name): self.name = name; self.calls = []
    async def list_sessions(self): self.calls.append("list_sessions"); return [self.name]
    async def usage(self, acct, *, timeout=None):  # mirrors a @rpc_deadline method
        self.calls.append(("usage", acct, timeout)); return {"acct": acct}
    setattr(usage, "__ccswitch_rpc_deadline__", True)
    def daemon_sha(self): return "abc1234"
    async def list_archived(self):
        self.calls.append("list_archived"); return [("archived", self.name)]


class _FakeRouter:
    def __init__(self, orchs): self._orchs = orchs
    def get(self, name): return self._orchs[name]
    def names(self): return sorted(self._orchs)
    async def pick(self, *, prefer=None): return sorted(self._orchs)


@pytest.mark.asyncio
async def test_unrouted_method_goes_to_primary():
    prim = _FakeOrch("rocky")
    fo = FleetOrchestrator(_FakeRouter({"rocky": prim}), primary="rocky")
    assert await fo.list_archived() == [("archived", "rocky")]
    assert prim.calls == ["list_archived"]


# --- list_sessions fans out across every host ---------------------------------
#
# The primary's _records only mirrors FOREIGN rows at rehydrate (process start), so
# a session a worker created after the primary's last restart is invisible to a
# primary-only list until the next restart — the "session disappeared into the
# void" report. The fleet list must ask every host and merge.

class _LRec:
    def __init__(self, id, host=None, status="idle", detached=False):
        self.id, self.host, self.status, self.detached = id, host, status, detached

    def __repr__(self):
        return f"_LRec({self.id!r}, host={self.host!r}, status={self.status!r})"


class _ListOrch(_FakeOrch):
    def __init__(self, name, recs=None, fail=None):
        super().__init__(name)
        self.recs, self.fail = list(recs or []), fail

    async def list_sessions(self):
        self.calls.append("list_sessions")
        if self.fail is not None:
            raise self.fail
        return list(self.recs)


@pytest.mark.asyncio
async def test_list_sessions_fans_out_to_every_host():
    dobby = _ListOrch("dobby", [_LRec("s-d1"), _LRec("s-d2")])
    rocky = _ListOrch("rocky", [_LRec("s-r1")])
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")
    got = await fo.list_sessions()
    assert sorted(r.id for r in got) == ["s-d1", "s-d2", "s-r1"]
    assert dobby.calls == ["list_sessions"] and rocky.calls == ["list_sessions"]


@pytest.mark.asyncio
async def test_list_sessions_prefers_owning_host_over_primary_mirror():
    # The primary holds a stale mirror of a rocky-owned session; the worker's live
    # record (the one with the driver) must win, and the result must not duplicate.
    dobby = _ListOrch("dobby", [_LRec("s-r1", status="idle"), _LRec("s-d1")])
    rocky = _ListOrch("rocky", [_LRec("s-r1", status="in_progress")])
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")
    got = {r.id: r for r in await fo.list_sessions()}
    assert set(got) == {"s-r1", "s-d1"}
    assert got["s-r1"].status == "in_progress"


@pytest.mark.asyncio
async def test_list_sessions_drops_primary_mirror_the_owning_host_no_longer_lists():
    # "Zombie session": rocky archived (or deleted / closed) s-r1 and popped its own
    # _records, but the primary's rehydrate-time mirror copy lingers until the
    # two-strike mirror reconcile (20–40 s, unbounded during a DB blip). During
    # that window the fleet list re-surfaced the card — every action on it routed
    # to rocky and 404'd "Session not found". The host that RUNS a session is
    # authoritative: it answered and did not list s-r1, so the driverless mirror
    # must not stand in for it.
    dobby = _ListOrch("dobby", [_LRec("s-r1", host="rocky", detached=True), _LRec("s-d1")])
    rocky = _ListOrch("rocky", [_LRec("s-r2", host="rocky")])
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")
    got = {r.id: r for r in await fo.list_sessions()}
    assert set(got) == {"s-d1", "s-r2"}


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_first", [True, False])
async def test_list_sessions_unarchive_on_owner_yields_one_live_record(primary_first):
    # #1033 Q3: a zombie card can be UNARCHIVED through the per-session route while
    # the primary still holds its stale mirror. The owner then lists a fresh, live
    # record for the same id while the primary lists an archived one — and the merge
    # must collapse them to exactly one record, the owner's, in either fan-out order
    # (the mirror is neither dropped as stale nor allowed to shadow the live row).
    dobby = _ListOrch("dobby", [_LRec("s-r1", host="rocky",
                                      status="archived", detached=True)])
    rocky = _ListOrch("rocky", [_LRec("s-r1", host="rocky",
                                      status="in_progress", detached=False)])
    hosts = ({"dobby": dobby, "rocky": rocky} if primary_first
             else {"rocky": rocky, "dobby": dobby})
    fo = FleetOrchestrator(_FakeRouter(hosts), primary="dobby")
    got = await fo.list_sessions()
    assert len(got) == 1, "the mirror and the owner's row must collapse to one record"
    assert got[0].status == "in_progress"
    assert fo._host_cache["s-r1"] == "rocky"  # actions route to the owner, not the mirror


@pytest.mark.asyncio
async def test_list_sessions_keeps_primary_mirror_when_owning_host_is_unreachable():
    # The mirror is the only view we have of a session on a dead worker — keep it
    # (the existing "dead worker must not blank the dashboard" contract).
    dobby = _ListOrch("dobby", [_LRec("s-r1", host="rocky", detached=True)])
    rocky = _ListOrch("rocky", fail=ConnectionError("rocky down"))
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")
    assert [r.id for r in await fo.list_sessions()] == ["s-r1"]


@pytest.mark.asyncio
async def test_list_sessions_keeps_primary_record_with_live_driver_despite_foreign_stamp():
    # A foreign-stamped record the primary actually drives (ensure_driver adopted
    # it) is live, not a mirror — never hide a session that has a driver.
    dobby = _ListOrch("dobby", [_LRec("s-r1", host="rocky", detached=False)])
    rocky = _ListOrch("rocky", [])
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")
    assert [r.id for r in await fo.list_sessions()] == ["s-r1"]


@pytest.mark.asyncio
async def test_list_sessions_stamps_host_when_record_has_none():
    # A worker's own automation spawn carried host=NULL (pre-fix rows). The fleet
    # list labels it with the host that actually served it so the UI can show
    # where it runs; records that already carry a host are left alone.
    dobby = _ListOrch("dobby", [_LRec("s-d1")])
    rocky = _ListOrch("rocky", [_LRec("s-r1"), _LRec("s-r2", host="rocky")])
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")
    got = {r.id: r for r in await fo.list_sessions()}
    assert got["s-r1"].host == "rocky"
    assert got["s-r2"].host == "rocky"
    assert got["s-d1"].host == "dobby"
    # Per-session routing learns the real owner too: a pre-stamp NULL-host row would
    # otherwise route get_session/answer_question to the primary (→ 404 / no driver).
    assert fo._host_cache["s-r1"] == "rocky"
    assert "s-d1" not in fo._host_cache


@pytest.mark.asyncio
async def test_list_sessions_tolerates_unreachable_worker():
    # A dead worker must not blank the whole dashboard.
    dobby = _ListOrch("dobby", [_LRec("s-d1")])
    rocky = _ListOrch("rocky", fail=ConnectionError("rocky down"))
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")
    got = await fo.list_sessions()
    assert [r.id for r in got] == ["s-d1"]


@pytest.mark.asyncio
async def test_list_sessions_primary_failure_still_raises():
    # The primary's failure is the fleet's failure: keep the existing contract so
    # the API surfaces "daemon unavailable" instead of a silently partial list.
    dobby = _ListOrch("dobby", fail=ConnectionError("primary down"))
    rocky = _ListOrch("rocky", [_LRec("s-r1")])
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")
    with pytest.raises(ConnectionError):
        await fo.list_sessions()


@pytest.mark.asyncio
async def test_primary_method_preserves_rpc_deadline_marker():
    prim = _FakeOrch("rocky")
    fo = FleetOrchestrator(_FakeRouter({"rocky": prim}), primary="rocky")
    # The exposed callable must still be recognised as native-deadline-capable,
    # or call_with_deadline cancels the socket (#734).
    assert getattr(fo.usage, _DEADLINE_ATTR, False) is True


def test_sync_cached_read_passes_through():
    prim = _FakeOrch("rocky")
    fo = FleetOrchestrator(_FakeRouter({"rocky": prim}), primary="rocky")
    assert fo.daemon_sha() == "abc1234"


@pytest.mark.asyncio
async def test_per_session_op_routes_to_owning_host(monkeypatch):
    rocky, dobby = _FakeOrch("rocky"), _FakeOrch("dobby")

    async def get_session(self, sid):  # per-session method on the fakes
        self.calls.append(("get_session", sid)); return {"id": sid, "host": self.name}
    _FakeOrch.get_session = get_session

    fo = FleetOrchestrator(_FakeRouter({"rocky": rocky, "dobby": dobby}), primary="rocky")

    async def fake_host_for(sid):  # stand in for the DB lookup
        return {"s-on-dobby": "dobby", "s-on-rocky": "rocky"}.get(sid)
    monkeypatch.setattr(fo, "_host_for", fake_host_for)

    await fo.get_session("s-on-dobby")
    await fo.get_session("s-on-rocky")
    assert ("get_session", "s-on-dobby") in dobby.calls
    assert ("get_session", "s-on-rocky") in rocky.calls


@pytest.mark.asyncio
async def test_per_session_marked_method_keeps_deadline_marker():
    fo = FleetOrchestrator(_FakeRouter({"rocky": _FakeOrch("rocky")}), primary="rocky")
    assert getattr(fo.ensure_driver, _DEADLINE_ATTR, False) is True


@pytest.mark.asyncio
async def test_unknown_host_falls_back_to_primary(monkeypatch):
    rocky = _FakeOrch("rocky")

    async def get_session(self, sid): self.calls.append(("get_session", sid)); return {}
    _FakeOrch.get_session = get_session
    fo = FleetOrchestrator(_FakeRouter({"rocky": rocky}), primary="rocky")
    monkeypatch.setattr(fo, "_host_for", lambda sid: _acoro(None))
    await fo.get_session("orphan")
    assert ("get_session", "orphan") in rocky.calls


async def _acoro(v): return v


class _Rec:
    def __init__(self, id): self.id = id; self.host = None

@pytest.mark.asyncio
async def test_create_picks_top_host_and_stamps(monkeypatch):
    rocky, dobby = _FakeOrch("rocky"), _FakeOrch("dobby")
    async def create_session(self, **kw): return _Rec(f"sess-{self.name}")
    _FakeOrch.create_session = create_session
    router = _FakeRouter({"rocky": rocky, "dobby": dobby})
    async def pick(*, prefer=None): return ["dobby", "rocky"]  # dobby ranked first
    router.pick = pick
    fo = FleetOrchestrator(router, primary="rocky")
    stamped = {}
    async def fake_stamp(sid, host): stamped[sid] = host
    monkeypatch.setattr(fo, "_stamp_host", fake_stamp)

    rec = await fo.create_session(account="a")
    assert rec.id == "sess-dobby" and rec.host == "dobby"
    assert stamped == {"sess-dobby": "dobby"}
    assert fo._host_cache["sess-dobby"] == "dobby"

@pytest.mark.asyncio
async def test_create_cascades_on_spawn_failure(monkeypatch):
    good = _FakeOrch("rocky")
    bad = _FakeOrch("dobby")
    async def ok(self, **kw): return _Rec("s1")
    async def boom(self, **kw): raise RuntimeError("spawn failed")
    bad.create_session = boom.__get__(bad); good.create_session = ok.__get__(good)
    router = _FakeRouter({"rocky": good, "dobby": bad})
    async def pick(*, prefer=None): return ["dobby", "rocky"]
    router.pick = pick
    fo = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fo, "_stamp_host", lambda sid, host: _acoro(None))
    rec = await fo.create_session()
    assert rec.id == "s1" and rec.host == "rocky"

@pytest.mark.asyncio
async def test_prefer_is_forwarded_not_passed_to_daemon(monkeypatch):
    rocky = _FakeOrch("rocky"); seen = {}
    async def create_session(self, **kw): seen.update(kw); return _Rec("s1")
    _FakeOrch.create_session = create_session
    router = _FakeRouter({"rocky": rocky}); got = {}
    async def pick(*, prefer=None): got["prefer"] = prefer; return ["rocky"]
    router.pick = pick
    fo = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fo, "_stamp_host", lambda sid, host: _acoro(None))
    await fo.create_session(prefer="rocky", account="a")
    assert got["prefer"] == "rocky"
    assert "prefer" not in seen and seen["account"] == "a"  # prefer never reaches daemon


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["openai", None])
async def test_create_rejects_invalid_engine_before_host_ranking(monkeypatch, engine):
    from server.sessions.engine import InvalidAgentEngine

    router = _FakeRouter({"rocky": _FakeOrch("rocky")})
    pick = mock.AsyncMock(return_value=["rocky"])
    router.pick = pick
    fleet = FleetOrchestrator(router, primary="rocky")

    with pytest.raises(InvalidAgentEngine):
        await fleet.create_session(engine=engine)

    pick.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_rejects_codex_before_host_ranking_until_capabilities_exist():
    from server.sessions.engine import AgentEngineUnavailable

    router = _FakeRouter({"rocky": _FakeOrch("rocky")})
    pick = mock.AsyncMock(return_value=["rocky"])
    router.pick = pick
    fleet = FleetOrchestrator(router, primary="rocky")

    with pytest.raises(AgentEngineUnavailable):
        await fleet.create_session(engine="codex")

    pick.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_omits_explicit_claude_engine_for_legacy_runner(monkeypatch):
    rocky = _FakeOrch("rocky")
    seen = {}

    async def create_session(self, **kwargs):
        seen.update(kwargs)
        return _Rec("s1")

    rocky.create_session = create_session.__get__(rocky)
    router = _FakeRouter({"rocky": rocky})
    router.pick = mock.AsyncMock(return_value=["rocky"])
    fleet = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fleet, "_stamp_host", lambda sid, host: _acoro(None))

    await fleet.create_session(engine="claude_code", account="a")

    assert seen == {"account": "a"}


@pytest.mark.asyncio
async def test_fanout_groups_ids_by_host_and_merges(monkeypatch):
    rocky, dobby = _FakeOrch("rocky"), _FakeOrch("dobby")
    async def many(self, ids, *, timeout=None, viewing=False):
        return {i: f"{self.name}:{i}" for i in ids}
    rocky.latest_summary_many = many.__get__(rocky)
    dobby.latest_summary_many = many.__get__(dobby)
    fo = FleetOrchestrator(_FakeRouter({"rocky": rocky, "dobby": dobby}), primary="rocky")
    async def host_for(sid): return "dobby" if sid.startswith("d") else "rocky"
    monkeypatch.setattr(fo, "_host_for", host_for)
    out = await fo.latest_summary_many(["r1", "d1", "r2"], timeout=1.0)
    assert out == {"r1": "rocky:r1", "d1": "dobby:d1", "r2": "rocky:r2"}

@pytest.mark.asyncio
async def test_fanout_keeps_deadline_marker():
    fo = FleetOrchestrator(_FakeRouter({"rocky": _FakeOrch("rocky")}), primary="rocky")
    assert getattr(fo.latest_summary_many, _DEADLINE_ATTR, False) is True


def test_resume_interrupted_in_per_session():
    """resume_interrupted must be classified as per-session so fleet routes it to
    the owning host rather than the primary (which has no driver for foreign sessions)."""
    from server.sessions.fleet_orchestrator import _PER_SESSION
    assert "resume_interrupted" in _PER_SESSION


class _FakeClient:
    """Minimal DaemonClient stand-in that records the most recent call."""
    def __init__(self, return_value):
        self._return_value = return_value
        self.last_call = None  # (method_name, kwargs)

    async def call(self, method, **kwargs):
        self.last_call = (method, kwargs)
        return self._return_value


@pytest.mark.asyncio
async def test_remote_orchestrator_resume_interrupted_rpc():
    """RemoteOrchestrator.resume_interrupted must proxy to the daemon and return its result."""
    from server.sessions.remote import RemoteOrchestrator
    client = _FakeClient("resumed")
    ro = RemoteOrchestrator(client)
    result = await ro.resume_interrupted("s1", "system")
    assert result == "resumed"
    assert client.last_call == ("resume_interrupted", {"session_id": "s1", "identity": "system"})


def test_every_remote_method_is_classified():
    import inspect
    from server.sessions.remote import RemoteOrchestrator
    from server.sessions.fleet_orchestrator import (
        _PER_SESSION, _FANOUT_BY_IDS, _MERGE_ALL_HOSTS)

    EXPLICIT = {"create_session", "subscribe", "aclose", "__init__"}
    # Global/primary-routed: everything not per-session, fan-out, merge-all, or
    # explicit. Listed so adding a NEW method forces a conscious classification here.
    GLOBAL = {
        "list_sessions", "list_archived", "pr_for", "prs_for", "all_prs",
        "all_repos_stage_models", "all_merge_trains", "latest_queue_entries",
        "check_insights", "ready_merge", "deploy_config_list", "deploy_config_put",
        "deploy_status", "git_credential_alerts", "dismiss_git_credential_alert",
        "automation_statuses", "automation_actionable", "automation_trigger",
        "pr_doctor_job", "resume_recovery_status", "consolidation_candidates",
        "automation_consolidate", "backlog_candidates", "backlog_runner_dispatch",
        "list_adoptable_candidates", "migrate_archive_home", "cloned_repos", "usage",
        "active_session_count",
        "copilot_add_start", "copilot_add_status", "copilot_add_cancel", "copilot_usage",
        "subscribe_events", "unsubscribe_events", "x11_metrics", "adopt_session",
        "daemon_sha", "daemon_pool_stats", "daemon_epoch", "startup_report",
        "maybe_start_queued",
        "pm_onboarding_counts", "pm_onboard_spawn_map", "pm_classify_run",
        "pm_advance_map_merged",
    }
    public = {n for n, _ in inspect.getmembers(RemoteOrchestrator, callable)
              if not n.startswith("_")}
    classified = (_PER_SESSION | _FANOUT_BY_IDS | set(_MERGE_ALL_HOSTS)
                  | EXPLICIT | GLOBAL)
    unclassified = public - classified
    assert not unclassified, f"unclassified RemoteOrchestrator methods: {sorted(unclassified)}"


# --- merge-all reads fan out to every host and reduce (#920) -------------------
#
# live_session_load / live_session_counts read the primary's _drivers ∩ _records;
# session_queue_positions read its queue. A primary-only answer silently drops
# every worker session, so the account selector stacked a worker-heavy account and
# the Accounts UI / queuePosition undercounted. These must ask every host and reduce.

class _AggOrch(_FakeOrch):
    def __init__(self, name, *, load=None, counts=None, positions=None, fail=False):
        super().__init__(name)
        self._load, self._counts, self._positions, self._fail = load, counts, positions, fail
    async def live_session_load(self):
        if self._fail: raise RuntimeError(f"{self.name} down")
        return self._load or {}
    async def live_session_counts(self):
        if self._fail: raise RuntimeError(f"{self.name} down")
        return self._counts or {}
    async def session_queue_positions(self):
        if self._fail: raise RuntimeError(f"{self.name} down")
        return self._positions or {}


@pytest.mark.asyncio
async def test_live_session_load_sums_across_hosts():
    dobby = _AggOrch("dobby", load={"acctA": 1.0, "acctB": 0.3})
    rocky = _AggOrch("rocky", load={"acctA": 2.0, "acctC": 1.0})
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")
    assert await fo.live_session_load() == {"acctA": 3.0, "acctB": 0.3, "acctC": 1.0}


@pytest.mark.asyncio
async def test_live_session_counts_sums_active_and_idle_buckets():
    dobby = _AggOrch("dobby", counts={"acctA": {"active": 1, "idle": 2}})
    rocky = _AggOrch("rocky", counts={"acctA": {"active": 3, "idle": 0},
                                      "acctB": {"active": 0, "idle": 1}})
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")
    assert await fo.live_session_counts() == {
        "acctA": {"active": 4, "idle": 2}, "acctB": {"active": 0, "idle": 1}}


@pytest.mark.asyncio
async def test_session_queue_positions_unions_across_hosts():
    dobby = _AggOrch("dobby", positions={"s1": 1, "s2": 2})
    rocky = _AggOrch("rocky", positions={"w1": 1})
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")
    assert await fo.session_queue_positions() == {"s1": 1, "s2": 2, "w1": 1}


@pytest.mark.asyncio
async def test_merge_all_skips_unreachable_worker_but_raises_on_primary():
    dobby = _AggOrch("dobby", load={"acctA": 1.0})
    rocky = _AggOrch("rocky", fail=True)  # a dead worker must not blank the total
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")
    assert await fo.live_session_load() == {"acctA": 1.0}
    # The primary's own failure still surfaces as "daemon unavailable".
    fo2 = FleetOrchestrator(_FakeRouter({"dobby": _AggOrch("dobby", fail=True)}), primary="dobby")
    with pytest.raises(RuntimeError, match="dobby down"):
        await fo2.live_session_load()


def test_merge_all_reads_mirror_remote_deadline_marker():
    # Marker propagation mirrors _make_per_session / _make_fanout: the fleet wrapper
    # carries @rpc_deadline iff the underlying RemoteOrchestrator method does.
    from server.sessions.remote import RemoteOrchestrator
    from server.sessions.fleet_orchestrator import _MERGE_ALL_HOSTS
    fo = FleetOrchestrator(_FakeRouter({"rocky": _AggOrch("rocky")}), primary="rocky")
    for name in _MERGE_ALL_HOSTS:
        expected = getattr(getattr(RemoteOrchestrator, name), _DEADLINE_ATTR, False)
        assert getattr(getattr(fo, name), _DEADLINE_ATTR, False) == expected


def test_api_orchestrator_reads_are_all_classified():
    """Every orchestrator method reached from server/api must be per-session,
    fanned out (by ids or merge-all), or explicitly whitelisted as primary-local.

    #920: a NEW fleet-wide read added to the API without a fan-out silently serves
    a primary-only (worker-blind) answer. This scans the API surface and forces a
    conscious classification for anything it touches.
    """
    import pathlib, re
    from server.sessions.fleet_orchestrator import (
        _PER_SESSION, _FANOUT_BY_IDS, _MERGE_ALL_HOSTS)

    # Whitelisted primary-local: reads whose truth already lives in the shared DB or
    # is intentionally primary-scoped (adoptable candidates, deploy status, …). A
    # fan-out here would be wrong or redundant, so they are named explicitly.
    PRIMARY_LOCAL = {
        "list_archived", "all_prs", "all_merge_trains", "all_repos_stage_models",
        "latest_queue_entries", "check_insights", "ready_merge", "deploy_config_list",
        "deploy_config_put", "deploy_status", "git_credential_alerts",
        "dismiss_git_credential_alert", "automation_statuses", "automation_actionable",
        "automation_trigger", "automation_consolidate", "consolidation_candidates",
        "backlog_candidates", "backlog_runner_dispatch", "list_adoptable_candidates",
        "adopt_session", "migrate_archive_home", "cloned_repos", "usage",
        "resume_recovery_status", "pr_doctor_job", "maybe_start_queued",
        "copilot_add_start", "copilot_add_status", "copilot_add_cancel",
        "pm_onboarding_counts", "pm_onboard_spawn_map", "pm_classify_run",
        "pm_advance_map_merged",
    }
    EXPLICIT = {"create_session", "list_sessions"}  # fleet-wide methods on FleetOrchestrator
    classified = (_PER_SESSION | _FANOUT_BY_IDS | set(_MERGE_ALL_HOSTS)
                  | PRIMARY_LOCAL | EXPLICIT)

    api_dir = pathlib.Path(__file__).resolve().parents[1] / "api"
    # Catch both `orch.<name>(` attribute calls and `getattr(orch, "<name>")` /
    # `state.sessions.<name>` lookups used in the API layer.
    pat = re.compile(r"(?:\borch|state\.sessions)\.([a-zA-Z_]\w*)"
                     r"|getattr\(\s*orch\s*,\s*[\"']([a-zA-Z_]\w*)[\"']")
    used: set[str] = set()
    for f in api_dir.glob("*.py"):
        for m in pat.finditer(f.read_text()):
            used.add(m.group(1) or m.group(2))
    unclassified = used - classified
    assert not unclassified, (
        f"server/api reaches unclassified orchestrator methods: {sorted(unclassified)} — "
        f"classify each as per-session, merge-all, or PRIMARY_LOCAL")
