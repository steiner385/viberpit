import pytest
from server.sessions.fleet_orchestrator import FleetOrchestrator
from server.sessions.facade import _DEADLINE_ATTR


class _FakeOrch:
    def __init__(self, name): self.name = name; self.calls = []
    async def list_sessions(self): self.calls.append("list_sessions"); return [self.name]
    async def usage(self, acct, *, timeout=None):  # mirrors a @rpc_deadline method
        self.calls.append(("usage", acct, timeout)); return {"acct": acct}
    setattr(usage, "__ccswitch_rpc_deadline__", True)
    def daemon_sha(self): return "abc1234"


class _FakeRouter:
    def __init__(self, orchs): self._orchs = orchs
    def get(self, name): return self._orchs[name]
    def names(self): return sorted(self._orchs)
    async def pick(self, *, prefer=None): return sorted(self._orchs)


@pytest.mark.asyncio
async def test_unrouted_method_goes_to_primary():
    prim = _FakeOrch("rocky")
    fo = FleetOrchestrator(_FakeRouter({"rocky": prim}), primary="rocky")
    assert await fo.list_sessions() == ["rocky"]
    assert prim.calls == ["list_sessions"]


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


def test_every_remote_method_is_classified():
    import inspect
    from server.sessions.remote import RemoteOrchestrator
    from server.sessions.fleet_orchestrator import _PER_SESSION, _FANOUT_BY_IDS

    EXPLICIT = {"create_session", "subscribe", "aclose", "__init__"}
    # Global/primary-routed: everything not per-session, fan-out, or explicit. Listed
    # so adding a NEW method forces a conscious classification here.
    GLOBAL = {
        "list_sessions", "list_archived", "pr_for", "prs_for", "all_prs",
        "all_repos_stage_models", "all_merge_trains", "latest_queue_entries",
        "check_insights", "ready_merge", "deploy_config_list", "deploy_config_put",
        "deploy_status", "git_credential_alerts", "dismiss_git_credential_alert",
        "automation_statuses", "automation_actionable", "automation_trigger",
        "pr_doctor_job", "resume_recovery_status", "consolidation_candidates",
        "automation_consolidate", "backlog_candidates", "backlog_runner_dispatch",
        "list_adoptable_candidates", "migrate_archive_home", "cloned_repos", "usage",
        "active_session_count", "live_session_load", "live_session_counts",
        "copilot_add_start", "copilot_add_status", "copilot_add_cancel", "copilot_usage",
        "subscribe_events", "unsubscribe_events", "x11_metrics", "adopt_session",
        "daemon_sha", "daemon_pool_stats", "daemon_epoch", "startup_report",
        "session_queue_positions", "maybe_start_queued",
        "pm_onboarding_counts", "pm_onboard_spawn_map", "pm_classify_run",
        "pm_advance_map_merged",
    }
    public = {n for n, _ in inspect.getmembers(RemoteOrchestrator, callable)
              if not n.startswith("_")}
    classified = _PER_SESSION | _FANOUT_BY_IDS | EXPLICIT | GLOBAL
    unclassified = public - classified
    assert not unclassified, f"unclassified RemoteOrchestrator methods: {sorted(unclassified)}"
