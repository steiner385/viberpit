import pytest
from unittest import mock
from server.sessions import host_upgrade as host_upgrade_mod
from server.sessions.engine import AgentEngine
from server.sessions.fleet_orchestrator import FleetOrchestrator
from server.sessions.facade import _DEADLINE_ATTR


class _FakeOrch:
    def __init__(self, name): self.name = name; self.calls = []
    async def list_sessions(self): self.calls.append("list_sessions"); return [self.name]
    async def usage(self, acct, *, timeout=None):  # mirrors a @rpc_deadline method
        self.calls.append(("usage", acct, timeout)); return {"acct": acct}
    setattr(usage, "__viberpit_rpc_deadline__", True)
    def daemon_sha(self): return "abc1234"
    async def list_archived(self):
        self.calls.append("list_archived"); return [("archived", self.name)]
    async def find_assistant(self, owner_identity):
        self.calls.append(("find_assistant", owner_identity))
        return f"assistant@{self.name}"


class _FakeRouter:
    def __init__(self, orchs): self._orchs = orchs
    def get(self, name): return self._orchs[name]
    def names(self): return sorted(self._orchs)
    async def pick(self, *, prefer=None, engine=None,
                   expected_contract_sha256=None, ignore_capacity=False): return sorted(self._orchs)


@pytest.mark.asyncio
async def test_unrouted_method_goes_to_primary():
    prim = _FakeOrch("rocky")
    fo = FleetOrchestrator(_FakeRouter({"rocky": prim}), primary="rocky")
    assert await fo.list_archived() == [("archived", "rocky")]
    assert prim.calls == ["list_archived"]


@pytest.mark.asyncio
async def test_find_assistant_asks_only_the_primary():
    """create_session pins assistant-kind sessions to the primary (its in-process MCP
    server and loopback API only exist there), so the owner lookup must resolve on the
    primary too. Fanning it out or routing it per-session would look for the thread on a
    worker that structurally cannot hold one, return None, and mint a duplicate over the
    existing transcript. This is the read half of that pinning invariant."""
    dobby, rocky = _FakeOrch("dobby"), _FakeOrch("rocky")
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")
    assert await fo.find_assistant("me@example.com") == "assistant@dobby"
    assert dobby.calls == [("find_assistant", "me@example.com")]
    assert rocky.calls == []


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
async def test_list_sessions_ignores_a_worker_copy_stamped_for_another_host():
    # 4a558770 (2026-10-09): dobby adopted a queued entry back from rocky, but
    # rocky kept a driverless copy. The worker-wins merge rule let that stale copy
    # shadow the primary's record and pinned every queue action to rocky, which
    # answered "not in the queue". A detached worker copy stamped for ANOTHER host
    # is not the session running there — the primary's record must win, and a
    # route an earlier list cached for the worker must be forgotten.
    dobby = _ListOrch("dobby", [_LRec("s-q", host="dobby", status="queued")])
    rocky = _ListOrch("rocky", [_LRec("s-q", host="rocky", status="queued", detached=True)])
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")
    await fo.list_sessions()
    assert fo._host_cache["s-q"] == "rocky"  # rocky's copy was its own back then

    rocky.recs = [_LRec("s-q", host="dobby", status="queued", detached=True)]
    got = {r.id: r for r in await fo.list_sessions()}
    assert got["s-q"].host == "dobby"
    assert "s-q" not in fo._host_cache  # _host_for re-reads the row's stamp


@pytest.mark.asyncio
async def test_list_sessions_forgets_a_worker_route_once_the_worker_stops_listing_it():
    dobby = _ListOrch("dobby", [_LRec("s-q", host="dobby", status="queued")])
    rocky = _ListOrch("rocky", [_LRec("s-q", host="rocky", status="queued", detached=True)])
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")
    await fo.list_sessions()
    rocky.recs = []
    await fo.list_sessions()
    assert "s-q" not in fo._host_cache


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
    async def pick(*, prefer=None, engine=None, expected_contract_sha256=None, ignore_capacity=False):
        return ["dobby", "rocky"]  # dobby ranked first
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
@pytest.mark.parametrize("kind,expected", [("assistant", True), ("coding", False), (None, False)])
async def test_create_exempts_only_the_assistant_from_the_capacity_leg(
        monkeypatch, kind, expected):
    """#2184: the assistant neither counts against nor queues behind the session cap
    (orchestrator.py's global-cap gate and `_consumes_capacity_slot` both exempt it),
    yet `pick` applied `active_sessions >= max_sessions` to it uniformly — so the
    drawer was refused for lack of a slot it never takes. Every other kind must keep
    respecting capacity."""
    rocky = _FakeOrch("rocky")
    async def create_session(self, **kw): return _Rec("s1")
    _FakeOrch.create_session = create_session
    router = _FakeRouter({"rocky": rocky})
    seen = {}
    async def pick(*, prefer=None, engine=None, expected_contract_sha256=None,
                   ignore_capacity=False):
        seen["ignore_capacity"] = ignore_capacity
        return ["rocky"]
    router.pick = pick
    fo = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fo, "_stamp_host", lambda sid, host: _acoro(None))

    await fo.create_session(**({"kind": kind} if kind is not None else {}))
    assert seen["ignore_capacity"] is expected


def _assistant_fleet(monkeypatch, *, order, primary_create):
    """A dobby(primary)+rocky fleet whose router ranks *order* and whose primary's
    create_session is *primary_create*; rocky always accepts, so any assistant that
    lands there proves the pin leaked."""
    dobby, rocky = _FakeOrch("dobby"), _FakeOrch("rocky")
    async def rocky_ok(self, **kw):
        self.calls.append("create_session"); return _Rec("on-rocky")
    rocky.create_session = rocky_ok.__get__(rocky)
    dobby.create_session = primary_create.__get__(dobby)
    router = _FakeRouter({"dobby": dobby, "rocky": rocky})
    async def pick(*, prefer=None, engine=None, expected_contract_sha256=None,
                   ignore_capacity=False):
        return list(order)
    router.pick = pick
    fo = FleetOrchestrator(router, primary="dobby")
    monkeypatch.setattr(fo, "_stamp_host", lambda sid, host: _acoro(None))
    return fo, rocky


@pytest.mark.asyncio
async def test_assistant_create_never_fails_over_when_primary_is_not_ranked(monkeypatch):
    """2026-10-09 outage: `prefer=primary` only REORDERED the candidates, so when the
    primary was briefly not dispatchable the drawer's thread was created on rocky.
    Rocky has no edge on 127.0.0.1:8787, so every assistant tool call failed with
    ConnectError for days while report_gap (DB-only) kept working. A worker can
    never serve an assistant, so no primary means refuse — never fall over."""
    from server.sessions.fleet_router import NoHostAvailable
    async def unused(self, **kw): raise AssertionError("primary was not ranked")
    fo, rocky = _assistant_fleet(monkeypatch, order=["rocky"], primary_create=unused)
    with pytest.raises(NoHostAvailable, match="primary"):
        await fo.create_session(kind="assistant")
    assert rocky.calls == []


@pytest.mark.asyncio
async def test_assistant_create_never_cascades_past_a_failing_primary(monkeypatch):
    """The other leak: primary ranked first but its spawn raised, and _dispatch's
    best-first loop moved on to the worker."""
    from server.sessions.fleet_router import NoHostAvailable
    async def boom(self, **kw): raise RuntimeError("spawn failed")
    fo, rocky = _assistant_fleet(monkeypatch, order=["dobby", "rocky"], primary_create=boom)
    with pytest.raises((NoHostAvailable, RuntimeError)):
        await fo.create_session(kind="assistant")
    assert rocky.calls == []


@pytest.mark.asyncio
async def test_coding_create_still_fails_over_past_the_primary(monkeypatch):
    """The strict pin is assistant-only: a coding session keeps best-first failover."""
    async def boom(self, **kw): raise RuntimeError("spawn failed")
    fo, rocky = _assistant_fleet(monkeypatch, order=["dobby", "rocky"], primary_create=boom)
    rec = await fo.create_session(kind="coding")
    assert rec.host == "rocky" and rocky.calls == ["create_session"]


@pytest.mark.asyncio
async def test_create_cascades_on_spawn_failure(monkeypatch):
    good = _FakeOrch("rocky")
    bad = _FakeOrch("dobby")
    async def ok(self, **kw): return _Rec("s1")
    async def boom(self, **kw): raise RuntimeError("spawn failed")
    bad.create_session = boom.__get__(bad); good.create_session = ok.__get__(good)
    router = _FakeRouter({"rocky": good, "dobby": bad})
    async def pick(*, prefer=None, engine=None, expected_contract_sha256=None, ignore_capacity=False):
        return ["dobby", "rocky"]
    router.pick = pick
    fo = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fo, "_stamp_host", lambda sid, host: _acoro(None))
    rec = await fo.create_session()
    assert rec.id == "s1" and rec.host == "rocky"

@pytest.mark.asyncio
async def test_create_does_not_cascade_when_the_first_host_outcome_is_unknown(monkeypatch):
    """A read timeout or mid-response reset means the first daemon MAY have finished
    the spawn — it did on 2026-09-07: the edge gave up at 30s and rocky's session row
    landed 2s later. Cascading to the next host then starts a SECOND session for the
    same prompt on another runner (the cross-host duplicate pairs of 2026-09-05).
    An unknown outcome must propagate to the caller, never fail over."""
    from server.ipc.client import DaemonTimeout
    slow, spare = _FakeOrch("rocky"), _FakeOrch("dobby")
    async def timed_out(self, **kw):
        self.calls.append("create_session")
        raise DaemonTimeout("create_session timed out", outcome_unknown=True)
    async def ok(self, **kw):
        self.calls.append("create_session")
        return _Rec("s2")
    slow.create_session = timed_out.__get__(slow); spare.create_session = ok.__get__(spare)
    router = _FakeRouter({"rocky": slow, "dobby": spare})
    async def pick(*, prefer=None, engine=None, expected_contract_sha256=None, ignore_capacity=False):
        return ["rocky", "dobby"]
    router.pick = pick
    fo = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fo, "_stamp_host", lambda sid, host: _acoro(None))
    with pytest.raises(DaemonTimeout):
        await fo.create_session(name="n")
    assert slow.calls == ["create_session"]
    assert spare.calls == []


@pytest.mark.asyncio
async def test_create_cascades_when_the_first_host_was_never_reached(monkeypatch):
    """The inverse: a connect failure (daemon down, never dialled) cannot have spawned
    anything, so failing over is exactly what the fleet is for."""
    from server.ipc.client import DaemonUnavailable
    down, spare = _FakeOrch("rocky"), _FakeOrch("dobby")
    async def unreachable(self, **kw):
        raise DaemonUnavailable("connection refused")
    async def ok(self, **kw):
        return _Rec("s2")
    down.create_session = unreachable.__get__(down); spare.create_session = ok.__get__(spare)
    router = _FakeRouter({"rocky": down, "dobby": spare})
    async def pick(*, prefer=None, engine=None, expected_contract_sha256=None, ignore_capacity=False):
        return ["rocky", "dobby"]
    router.pick = pick
    fo = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fo, "_stamp_host", lambda sid, host: _acoro(None))
    rec = await fo.create_session(name="n")
    assert rec.id == "s2" and rec.host == "dobby"


@pytest.mark.asyncio
async def test_prefer_is_forwarded_not_passed_to_daemon(monkeypatch):
    rocky = _FakeOrch("rocky"); seen = {}
    async def create_session(self, **kw): seen.update(kw); return _Rec("s1")
    _FakeOrch.create_session = create_session
    router = _FakeRouter({"rocky": rocky}); got = {}
    async def pick(*, prefer=None, engine=None, expected_contract_sha256=None, ignore_capacity=False):
        got["prefer"] = prefer
        return ["rocky"]
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
async def test_codex_launch_filters_candidates_by_engine_and_forwards_the_key(monkeypatch):
    """A Codex request reaches host ranking, carrying the engine and the edge's pin.

    Before capability advertisement existed this raised AgentEngineUnavailable
    unconditionally. Now the router does the excluding, so what must be asserted is
    that `pick` is TOLD which engine to filter for — a create that ranked hosts
    without the engine would happily pick a Claude-only runner.
    """
    from server.codex.schema import PINNED_CONTRACT_SHA256

    rocky = _FakeOrch("rocky")
    seen: dict = {}

    async def create_session(self, **kw):
        seen.update(kw)
        return _Rec("s1")

    _FakeOrch.create_session = create_session
    router = _FakeRouter({"rocky": rocky})
    pick = mock.AsyncMock(return_value=["rocky"])
    router.pick = pick
    fleet = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fleet, "_stamp_host", lambda sid, host: _acoro(None))

    await fleet.create_session(engine="codex")

    assert pick.await_args.kwargs["engine"] is AgentEngine.CODEX
    assert pick.await_args.kwargs["expected_contract_sha256"] == PINNED_CONTRACT_SHA256
    # The engine key must still reach the daemon: the host was chosen FOR codex, and
    # dropping it here would silently spawn a Claude session on it.
    assert seen["engine"] is AgentEngine.CODEX


@pytest.mark.asyncio
async def test_claude_launch_omits_the_engine_key_for_legacy_daemons(monkeypatch):
    """Claude is the legacy default, so the additive key is not sent at all.

    A protocol-v1 daemon rejects an unknown kwarg; omitting it is what keeps a
    mixed-version fleet dispatchable during the upgrade window.
    """
    rocky = _FakeOrch("rocky")
    seen: dict = {}

    async def create_session(self, **kw):
        seen.update(kw)
        return _Rec("s1")

    _FakeOrch.create_session = create_session
    router = _FakeRouter({"rocky": rocky})
    pick = mock.AsyncMock(return_value=["rocky"])
    router.pick = pick
    fleet = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fleet, "_stamp_host", lambda sid, host: _acoro(None))

    await fleet.create_session(engine="claude_code")

    assert pick.await_args.kwargs["engine"] is None
    assert "engine" not in seen


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


def test_certify_harvested_complete_in_per_session():
    """A harvested one-shot session is certified `complete` on the host that owns
    its driver: the primary's chained ingest forwards through the edge, and the
    fleet must route it by the row's host stamp — routed to the primary it would
    no-op on an empty `_records` (the 2026-09-23 fridgesheet escalation)."""
    from server.sessions.fleet_orchestrator import _PER_SESSION
    assert "certify_harvested_complete" in _PER_SESSION


@pytest.mark.asyncio
async def test_remote_orchestrator_certify_harvested_complete_rpc():
    from server.sessions.remote import RemoteOrchestrator
    client = _FakeClient(None)
    ro = RemoteOrchestrator(client)
    assert await ro.certify_harvested_complete("s1") is None
    assert client.last_call == ("certify_harvested_complete", {"session_id": "s1"})


@pytest.mark.asyncio
async def test_remote_orchestrator_resume_interrupted_rpc():
    """RemoteOrchestrator.resume_interrupted must proxy to the daemon and return its result."""
    from server.sessions.remote import SPAWN_RPC_TIMEOUT, RemoteOrchestrator
    client = _FakeClient("resumed")
    ro = RemoteOrchestrator(client)
    result = await ro.resume_interrupted("s1", "system")
    assert result == "resumed"
    assert client.last_call == (
        "resume_interrupted",
        {"session_id": "s1", "identity": "system", "timeout": SPAWN_RPC_TIMEOUT})


def test_every_remote_method_is_classified():
    import inspect
    from server.sessions.remote import RemoteOrchestrator
    from server.sessions.fleet_orchestrator import (
        _PER_SESSION, _FANOUT_BY_IDS, _MERGE_ALL_HOSTS)

    EXPLICIT = {"create_session", "subscribe", "aclose", "__init__",
                # Fleet-wide method defined on FleetOrchestrator itself: it must
                # report EVERY host's outcome rather than skip an unreachable one,
                # so it cannot use the merge-all helper.
                "codex_account_status", "codex_invalidate_credentials",
                "automation_statuses", "automation_overview",
                # Per-host credential-envelope push (#2629): only push_credentials
                # calls it, addressing each runner by name through the router.
                "creds_sync"}
    # Global/primary-routed: everything not per-session, fan-out, merge-all, or
    # explicit. Listed so adding a NEW method forces a conscious classification here.
    GLOBAL = {
        "list_sessions", "list_archived", "pr_for", "prs_for", "all_prs", "pr_detail",
        "all_repos_stage_models", "all_merge_trains", "latest_queue_entries",
        "check_insights", "ready_merge", "deploy_config_list", "deploy_config_put",
        "deploy_status", "git_credential_alerts", "dismiss_git_credential_alert",
        "capacity_readout", "automation_actionable", "automation_trigger",
        "pr_doctor_job", "resume_recovery_status", "consolidation_candidates",
        "automation_consolidate", "backlog_candidates", "backlog_runner_dispatch",
        "list_adoptable_candidates", "migrate_archive_home", "cloned_repos", "usage",
        "active_session_count",
        # Owner-scoped, and primary-routed on purpose: create_session pins assistant-kind
        # sessions to the primary, so no worker can hold the thread this looks for.
        # See test_find_assistant_asks_only_the_primary.
        "find_assistant",
        # Same reasoning as find_assistant just above, and used together with it:
        # post_notice's first arg is an identity, not a session_id, so it cannot
        # use _make_per_session's calling convention either (Task 8.4).
        "post_notice",
        "copilot_add_start", "copilot_add_status", "copilot_add_cancel",
        # Process-backed (Copilot / Codex) quota refresh: account-scoped, and a
        # cold read starts ONE process, so the primary answers rather than every host.
        "process_quota_refresh",
        # Codex: the model list and model+effort validation are account-scoped and
        # give the same answer on any host that can start the app-server, so asking
        # the primary starts ONE app-server instead of one per host.
        # codex_account_status has an explicit merge because it also performs one
        # edge-authoritative quota write; invalidation is EXPLICIT above.
        "codex_models", "codex_validate_model",
        # One-shot Codex-native prompt refinement (#1288): account-scoped and not
        # tied to any particular session, so the primary is asked like the codex_*
        # reads above. Fleet-wide multi-host routing is unimplemented (gated behind
        # VIBERPIT_CODEX_REFINE_ENABLED anyway) — see refine_prompt_codex's docstring.
        "refine_prompt_codex",
        "subscribe_events", "unsubscribe_events", "x11_metrics", "adopt_session",
        "daemon_sha", "daemon_pool_stats", "daemon_epoch", "startup_report",
        "maybe_start_queued",
        "pm_onboarding_counts", "pm_onboard_spawn_map", "pm_onboard_spawn_constitution", "pm_classify_run",
        "pm_advance_map_merged", "pm_advance_live",
        # Materialization apply (#992): initiative-scoped, not session-scoped, and
        # its truth is the shared DB plus the daemon's gh credential ladder. A
        # fan-out would run the SAME journaled GitHub writes once per host.
        "sdlc_materialize_apply",
        # Blocked-by removal (roadmap spec §8, #1472): same reasoning as
        # sdlc_materialize_apply — shared-DB truth plus the daemon's own gh
        # credential ladder and journal, so it must not fan out per host.
        "pm_remove_dependency", "pm_issue_decision",
        # Capability map-changes (roadmap spec §9, #1472): a sync factory for an
        # EDGE-LOCAL service — the pointer row and the docs are shared-DB/GitHub
        # truth, so a fan-out would repeat every GitHub write once per host.
        "pm_map_changes",
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
    def __init__(self, name, *, load=None, counts=None, positions=None, status=None, fail=False):
        super().__init__(name)
        self._load, self._counts, self._positions, self._fail = load, counts, positions, fail
        self._status = status or {}
        self.codex_persist_flags: list[bool] = []
    async def live_session_load(self):
        if self._fail: raise RuntimeError(f"{self.name} down")
        return self._load or {}
    async def live_session_counts(self):
        if self._fail: raise RuntimeError(f"{self.name} down")
        return self._counts or {}
    async def session_queue_positions(self):
        if self._fail: raise RuntimeError(f"{self.name} down")
        return self._positions or {}
    async def codex_account_status(self, account, *, persist=True, force=False):
        self.codex_persist_flags.append(persist)
        return self._status


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
async def test_codex_status_fanout_has_one_edge_writer_and_no_runner_writer(monkeypatch):
    quota = {
        "v": 2,
        "provider": "openai",
        "fetched_at": "2026-09-05T12:00:00+00:00",
        "availability": "available",
        "extra_usage_enabled": False,
        "windows": [],
    }
    dobby = _AggOrch(
        "dobby", status={"quota": quota, "activeRunners": 0, "degradedReason": None}
    )
    rocky = _AggOrch(
        "rocky", status={"quota": quota, "activeRunners": 1, "degradedReason": None}
    )
    writes: list[tuple[str, str]] = []

    def _write(account, snapshot):
        writes.append((account, snapshot.provider))

    import server.sessions.fleet_orchestrator as fleet_mod

    monkeypatch.setattr(fleet_mod, "persist_quota_snapshot", _write, raising=False)
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")

    result = await fo.codex_account_status("chatgpt-pro")

    assert result["activeRunners"] == 1
    assert dobby.codex_persist_flags == [False]
    assert rocky.codex_persist_flags == [False]
    assert writes == [("chatgpt-pro", "openai")]


@pytest.mark.asyncio
async def test_codex_status_fanout_returns_but_never_persists_a_degraded_merge():
    """Every host answered degraded (2026-09-07: rocky's app-server client had
    latched closed, dobby had no runner). The page must still see the honest
    "unknown", but writing it to the ledger every 30 s poll kept the entry
    fresh, so no runner ever re-read and the account never recovered. Uses the
    real persist helper: the refusal lives there, not in this caller."""
    from viberpit import account_registry, ledger
    from tests.helpers import temp_env

    degraded = {
        "v": 2, "provider": "openai", "fetched_at": "2026-09-07T18:56:05+00:00",
        "availability": "unknown", "extra_usage_enabled": False, "windows": [],
    }
    dobby = _AggOrch("dobby", status={"quota": degraded, "activeRunners": 0,
                                      "degradedReason": None})
    rocky = _AggOrch("rocky", status={"quota": degraded, "activeRunners": 1,
                                      "degradedReason": None})
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")
    with temp_env():
        account_registry.register_account("chatgpt-pro", account_registry.PROVIDER_OPENAI,
                                          engine="codex")
        result = await fo.codex_account_status("chatgpt-pro")
        assert result["quota"]["availability"] == "unknown"
        assert result["activeRunners"] == 1
        with ledger.locked_ledger() as led:
            assert "usage" not in led["accounts"].get("chatgpt-pro", {})


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
        "list_archived", "all_prs", "pr_detail", "all_merge_trains", "all_repos_stage_models",
        "latest_queue_entries", "check_insights", "ready_merge", "deploy_config_list",
        "deploy_config_put", "deploy_status", "git_credential_alerts",
        "dismiss_git_credential_alert", "capacity_readout", "automation_actionable",
        "automation_trigger", "automation_consolidate", "consolidation_candidates",
        "backlog_candidates", "backlog_runner_dispatch", "list_adoptable_candidates",
        "adopt_session", "migrate_archive_home", "cloned_repos", "usage",
        "resume_recovery_status", "pr_doctor_job", "maybe_start_queued",
        "copilot_add_start", "copilot_add_status", "copilot_add_cancel",
        "process_quota_refresh",
        "pm_onboarding_counts", "pm_onboard_spawn_map", "pm_onboard_spawn_constitution", "pm_classify_run",
        "pm_advance_map_merged", "pm_advance_live",
        # Initiative-scoped human-gate apply (#992) — see the GLOBAL-set comment
        # in the sibling test: fanning it out would repeat every GitHub write.
        "sdlc_materialize_apply",
        # Blocked-by removal (roadmap spec §8, #1472): same reasoning as
        # sdlc_materialize_apply — shared-DB truth plus the daemon's own gh
        # credential ladder and journal, so it must not fan out per host.
        "pm_remove_dependency", "pm_issue_decision",
        # Capability map-changes (roadmap spec §9, #1472): a sync factory for an
        # EDGE-LOCAL service — the pointer row and the docs are shared-DB/GitHub
        # truth, so a fan-out would repeat every GitHub write once per host.
        "pm_map_changes",
        # Account-scoped and identical on every host: the primary answers, and one
        # app-server is started rather than one per host.
        "codex_models", "codex_validate_model",
        # One-shot Codex-native prompt refinement (#1288), primary-routed like the
        # codex_* reads above — see the GLOBAL-set comment in the sibling test.
        "refine_prompt_codex",
    }
    EXPLICIT = {"create_session", "list_sessions", "codex_account_status",
                "codex_invalidate_credentials", "automation_statuses",
                "automation_overview"}  # fleet-wide on FleetOrchestrator
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


@pytest.mark.asyncio
async def test_local_client_prefers_its_own_host(monkeypatch):
    rocky, dobby = _FakeOrch("rocky"), _FakeOrch("dobby")
    async def create_session(self, **kw): return _Rec(f"sess-{self.name}")
    _FakeOrch.create_session = create_session
    router = _FakeRouter({"rocky": rocky, "dobby": dobby}); got = {}
    async def pick(*, prefer=None, engine=None, expected_contract_sha256=None, ignore_capacity=False):
        got["prefer"] = prefer; return ["dobby", "rocky"]
    router.pick = pick
    fo = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fo, "_stamp_host", lambda sid, host: _acoro(None))
    await fo.create_session(client_node="dobby")
    assert got["prefer"] == "dobby"


@pytest.mark.asyncio
async def test_explicit_host_beats_the_nudge(monkeypatch):
    """Pins the ``prefer is None`` guard: client_node="dobby" is a fleet host and
    WOULD trigger the nudge on its own, but an explicit prefer must win. If the
    ``prefer is None`` check were dropped, the nudge would clobber "rocky" with
    "dobby" and this would fail."""
    rocky, dobby = _FakeOrch("rocky"), _FakeOrch("dobby")
    async def create_session(self, **kw): return _Rec("s")
    _FakeOrch.create_session = create_session
    router = _FakeRouter({"rocky": rocky, "dobby": dobby}); got = []
    async def pick(*, prefer=None, engine=None, expected_contract_sha256=None, ignore_capacity=False):
        got.append(prefer); return ["rocky"]
    router.pick = pick
    fo = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fo, "_stamp_host", lambda sid, host: _acoro(None))
    await fo.create_session(prefer="rocky", client_node="dobby")
    assert got == ["rocky"]


@pytest.mark.asyncio
async def test_explicit_virtual_suppresses_the_nudge(monkeypatch):
    """Pins the ``display != "virtual"`` guard: client_node="dobby" is a fleet
    host and prefer is None, so the nudge WOULD fire, unless the explicit
    "virtual" request suppresses it. If that guard were dropped, prefer would
    become "dobby" and this would fail."""
    rocky, dobby = _FakeOrch("rocky"), _FakeOrch("dobby")
    async def create_session(self, **kw): return _Rec("s")
    _FakeOrch.create_session = create_session
    router = _FakeRouter({"rocky": rocky, "dobby": dobby}); got = []
    async def pick(*, prefer=None, engine=None, expected_contract_sha256=None, ignore_capacity=False):
        got.append(prefer); return ["rocky"]
    router.pick = pick
    fo = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fo, "_stamp_host", lambda sid, host: _acoro(None))
    await fo.create_session(client_node="dobby", display="virtual")
    assert got == [None]


@pytest.mark.asyncio
async def test_non_fleet_client_node_does_not_nudge(monkeypatch):
    """Pins the ``client_node in self._router.names()`` guard: prefer is None
    and display is not "virtual", so the nudge WOULD fire on any client_node,
    unless "laptop" (not a fleet host) is excluded. If that membership check
    were dropped, prefer would become "laptop" and this would fail."""
    rocky, dobby = _FakeOrch("rocky"), _FakeOrch("dobby")
    async def create_session(self, **kw): return _Rec("s")
    _FakeOrch.create_session = create_session
    router = _FakeRouter({"rocky": rocky, "dobby": dobby}); got = []
    async def pick(*, prefer=None, engine=None, expected_contract_sha256=None, ignore_capacity=False):
        got.append(prefer); return ["rocky"]
    router.pick = pick
    fo = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fo, "_stamp_host", lambda sid, host: _acoro(None))
    await fo.create_session(client_node="laptop")          # not a fleet host
    assert got == [None]


@pytest.mark.asyncio
async def test_client_node_match_is_case_insensitive(monkeypatch):
    """The tailnet node name and the runner-registry name are one host spelled by
    two independent sources, so the nudge must match the way resolve_display_mode
    does — case-insensitively. Otherwise a "Dobby" client is routed native by the
    policy and placed on some other host by the router, and the window opens on a
    desktop nobody is sitting at. `prefer` must carry the REGISTRY's spelling,
    since that is the key FleetRouter.get() looks up."""
    rocky, dobby = _FakeOrch("rocky"), _FakeOrch("dobby")
    async def create_session(self, **kw): return _Rec("s")
    _FakeOrch.create_session = create_session
    router = _FakeRouter({"rocky": rocky, "dobby": dobby}); got = []
    async def pick(*, prefer=None, engine=None, expected_contract_sha256=None, ignore_capacity=False):
        got.append(prefer); return ["dobby", "rocky"]
    router.pick = pick
    fo = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fo, "_stamp_host", lambda sid, host: _acoro(None))
    await fo.create_session(client_node="Dobby")
    assert got == ["dobby"]


# ---------------------------------------------------------------------------
# A host the registry knows is unreachable must not be CALLED at all.
#
# The result was never the problem: an unreachable host is already skipped, just
# 13.04s later, after its connect timeout and retry (2026-09-09, rocky powered off
# for 17h, on an endpoint the UI polls every 5s). These pin that the fan-out asks
# the router which hosts are worth calling, and that what comes back is identical
# to the timeout path — same sessions, none of the wait.
# ---------------------------------------------------------------------------

class _ReachRouter(_FakeRouter):
    """Router that reports some hosts unreachable, and records what was asked."""

    def __init__(self, orchs, reachable):
        super().__init__(orchs)
        self._reachable = reachable
        self.asked_always = None

    async def reachable_names(self, *, always=None):
        self.asked_always = always
        return sorted(self._reachable)


@pytest.mark.asyncio
async def test_list_sessions_does_not_call_a_known_unreachable_host():
    live, dead = _ListOrch("dobby"), _ListOrch("rocky")
    router = _ReachRouter({"dobby": live, "rocky": dead}, reachable={"dobby"})
    fo = FleetOrchestrator(router, primary="dobby")

    await fo.list_sessions()

    assert dead.calls == [], "the powered-off host was still dialled — that is the 13s"
    assert live.calls == ["list_sessions"]
    assert router.asked_always == "dobby", "the primary must be exempt from skipping"


@pytest.mark.asyncio
async def test_skipping_yields_the_same_sessions_as_waiting_for_the_timeout():
    """Behaviour-preserving: this changes latency, not the result set."""
    class _Boom(_ListOrch):
        async def list_sessions(self, **kw):
            self.calls.append("list_sessions")
            raise OSError("connect timed out")

    slow = FleetOrchestrator(
        _FakeRouter({"dobby": _ListOrch("dobby"), "rocky": _Boom("rocky")}), primary="dobby")
    fast = FleetOrchestrator(
        _ReachRouter({"dobby": _ListOrch("dobby"), "rocky": _Boom("rocky")},
                     reachable={"dobby"}), primary="dobby")

    assert ([r.id for r in await slow.list_sessions()]
            == [r.id for r in await fast.list_sessions()])


@pytest.mark.asyncio
async def test_a_router_without_the_method_still_works():
    """Mixed-version safety: degrade to asking everyone, never to asking no one."""
    a, b = _ListOrch("dobby"), _ListOrch("rocky")
    fo = FleetOrchestrator(_FakeRouter({"dobby": a, "rocky": b}), primary="dobby")

    await fo.list_sessions()

    assert a.calls and b.calls, "fell back to no hosts instead of all hosts"


@pytest.mark.asyncio
async def test_unreachable_worker_logs_info_not_warning_while_its_restart_is_expected(caplog):
    """#1614: a worker mid-deploy is unreachable on purpose. Skipping it is still right,
    but a WARNING per failed poll turns every deploy into an Error Triage issue."""
    import logging
    from server.sessions import host_upgrade

    dobby = _AggOrch("dobby", load={"acctA": 1.0})
    rocky = _AggOrch("rocky", fail=True)
    fo = FleetOrchestrator(_FakeRouter({"dobby": dobby, "rocky": rocky}), primary="dobby")

    async def expected(name, **_):
        return name == "rocky"

    with mock.patch.object(host_upgrade, "restart_expected", expected), \
         caplog.at_level(logging.INFO, logger="server.sessions.fleet_orchestrator"):
        assert await fo.live_session_load() == {"acctA": 1.0}
    rocky_lines = [r for r in caplog.records if "host=rocky" in r.getMessage()]
    assert rocky_lines, "the skip must still be logged"
    assert all(r.levelno == logging.INFO for r in rocky_lines), \
        [(r.levelname, r.getMessage()) for r in rocky_lines]
    assert all("restart in progress" in r.getMessage() for r in rocky_lines)


@pytest.mark.asyncio
async def test_unreachable_worker_still_warns_when_no_restart_is_expected(caplog):
    import logging
    from server.sessions import host_upgrade

    fo = FleetOrchestrator(_FakeRouter({"dobby": _AggOrch("dobby", load={}),
                                        "rocky": _AggOrch("rocky", fail=True)}), primary="dobby")

    async def not_expected(name, **_):
        return False

    with mock.patch.object(host_upgrade, "restart_expected", not_expected), \
         caplog.at_level(logging.INFO, logger="server.sessions.fleet_orchestrator"):
        await fo.live_session_load()
    rocky_lines = [r for r in caplog.records
                   if r.getMessage() == "fleet.host_unreachable" and r.fields.get("host") == "rocky"]
    assert rocky_lines and all(r.levelno == logging.WARNING for r in rocky_lines)


# --- one unreachable host is ONE record family --------------------------------
#
# Five open auto-error issues, one cause: rocky was powered off. #1614
# (codex_account_status), #1670 (session_queue_positions), #1671 (list_sessions),
# #1733 (live_session_load), #1734 (live_session_counts). The fan-out site name sat
# in the message TEXT, and the sink fingerprints a record on its normalized message
# (server/obs/error_signature.py), so each site hashed differently and filed its own
# ticket — each carrying 60 lines of httpx transport boilerplate, and each re-filed
# on every pass of the ~30 s gap before the probe marks the host unhealthy.
#
# The host name itself turned out to be the same shape (#2126): unlike the quoted
# site/cause, it rode the message unquoted and survived normalization, so this
# whole call site still minted one issue per HOST. `log_event_throttled` fixes
# that the same way — fingerprint on the event name, host/site/cause in `fields`.

class _DownOrch(_FakeOrch):
    """A worker that refuses every no-id fan-out site while ``up`` is False."""

    def __init__(self, name, exc):
        super().__init__(name)
        self.exc, self.up = exc, False

    def _answer(self, value):
        if not self.up:
            raise self.exc
        return value

    async def list_sessions(self, **kw): return self._answer([])
    async def live_session_load(self): return self._answer({})
    async def live_session_counts(self): return self._answer({})
    async def session_queue_positions(self): return self._answer({})
    async def codex_account_status(self, account, *, persist=True, force=False):
        return self._answer({})


def _unplanned_outage():
    """No deploy in flight, so an unreachable host is a genuine surprise."""
    async def not_expected(name, **_):
        return False
    return mock.patch.object(host_upgrade_mod, "restart_expected", not_expected)


class _UpOrch(_AggOrch):
    """A primary that answers every no-id fan-out site with a well-formed result."""

    async def list_sessions(self, **kw):
        return [_LRec("s-d1", host=self.name)]


def _fleet_with_down_worker(exc):
    down = _DownOrch("rocky", exc)
    fo = FleetOrchestrator(
        _FakeRouter({"dobby": _UpOrch("dobby", load={"acctA": 1.0}), "rocky": down}),
        primary="dobby")
    return fo, down


def _rocky_records(caplog):
    return [r for r in caplog.records
            if r.getMessage() == "fleet.host_unreachable" and r.fields.get("host") == "rocky"]


@pytest.fixture
def sustained_outage(monkeypatch):
    """The probe has already seen rocky miss enough times that a miss is news (#2821)."""
    from server.sessions import host_probe
    monkeypatch.setitem(host_probe._CONSECUTIVE_UNREACHABLE, "rocky",
                        host_probe.UNREACHABLE_ESCALATE_AFTER)


@pytest.mark.asyncio
async def test_every_fanout_site_shares_one_unreachable_fingerprint(caplog, sustained_outage):
    """The fix for the five-issues shape, asserted through the REAL sink signature.

    Not "the messages look similar" — the thing that decides how many issues get
    filed is ``signature_of``, so that is what this calls.
    """
    import logging
    from server.ipc.client import DaemonUnavailable
    from server.obs.error_signature import signature_of

    exc = DaemonUnavailable("All connection attempts failed")
    sites = [
        lambda fo: fo.live_session_load(),
        lambda fo: fo.live_session_counts(),
        lambda fo: fo.session_queue_positions(),
        lambda fo: fo.list_sessions(),
        lambda fo: fo.codex_account_status("chatgpt-pro"),
    ]
    with _unplanned_outage(), \
         caplog.at_level(logging.DEBUG, logger="server.sessions.fleet_orchestrator"):
        for site in sites:
            # A fresh fleet per site: each must report at full level, and the
            # throttle is per-instance so this also pins that it is not global.
            await site(_fleet_with_down_worker(exc)[0])

    recs = _rocky_records(caplog)
    assert len(recs) == len(sites), [r.fields for r in recs]
    assert all(r.levelno == logging.WARNING for r in recs)
    fingerprints = {signature_of(r, source="edge", host="dobby").fingerprint
                    for r in recs}
    assert len(fingerprints) == 1, [r.fields for r in recs]
    # The site is still readable — collapsed in the fingerprint, kept in the record.
    assert {"live_session_load", "list_sessions", "codex_account_status"} <= {
        r.fields["site"] for r in recs}


@pytest.mark.asyncio
async def test_a_transport_failure_is_summarized_instead_of_carrying_its_traceback(caplog, sustained_outage):
    """httpx's stack says how it failed to connect; the typed exception says that it
    did, which is the whole diagnosis. Only an UNEXPECTED type keeps its stack."""
    import logging
    from server.ipc.client import DaemonUnavailable

    with _unplanned_outage(), \
         caplog.at_level(logging.DEBUG, logger="server.sessions.fleet_orchestrator"):
        await _fleet_with_down_worker(
            DaemonUnavailable("All connection attempts failed"))[0].live_session_load()
    rec = _rocky_records(caplog)[-1]
    assert rec.exc_info is None
    assert rec.fields["cause"] == "DaemonUnavailable: All connection attempts failed"

    caplog.clear()
    with _unplanned_outage(), \
         caplog.at_level(logging.DEBUG, logger="server.sessions.fleet_orchestrator"):
        await _fleet_with_down_worker(ValueError("merge blew up"))[0].live_session_load()
    rec = _rocky_records(caplog)[-1]
    assert rec.exc_info is not None, "an unexpected exception is a bug — keep the stack"


@pytest.mark.asyncio
async def test_repeat_passes_are_demoted_below_the_error_sink(caplog, sustained_outage):
    """The sink captures WARNING+ only, so a demoted repeat stays in the journal at
    DEBUG and out of the issue — the heartbeat returns when the window expires."""
    import logging
    from server.ipc.client import DaemonUnavailable

    fo, _down = _fleet_with_down_worker(DaemonUnavailable("connect refused"))
    with _unplanned_outage(), \
         caplog.at_level(logging.DEBUG, logger="server.sessions.fleet_orchestrator"):
        for _ in range(3):
            await fo.live_session_load()

    assert [r.levelno for r in _rocky_records(caplog)] == [
        logging.WARNING, logging.DEBUG, logging.DEBUG]


@pytest.mark.asyncio
async def test_a_recovered_host_reports_its_next_outage_promptly(caplog, sustained_outage):
    """A window must pace a CONTINUING condition, never a new one: rocky coming back
    ends the outage, so the next one is news again even inside the same window."""
    import logging
    from server.ipc.client import DaemonUnavailable

    fo, down = _fleet_with_down_worker(DaemonUnavailable("connect refused"))
    with _unplanned_outage(), \
         caplog.at_level(logging.DEBUG, logger="server.sessions.fleet_orchestrator"):
        await fo.live_session_load()
        down.up = True
        await fo.live_session_load()
        down.up = False
        await fo.live_session_load()

    assert [r.levelno for r in _rocky_records(caplog)] == [
        logging.WARNING, logging.WARNING]


@pytest.mark.asyncio
async def test_automation_overview_asks_primary_once_without_worker_actionable_counts():
    primary, worker = _FakeOrch("dobby"), _FakeOrch("rocky")
    expected = {"statuses": {}, "actionableCounts": {"pr-doctor": None}}
    primary.automation_overview = mock.AsyncMock(return_value=expected)
    worker.automation_overview = mock.AsyncMock(side_effect=AssertionError("must not fan out"))
    worker.automation_statuses = mock.AsyncMock(return_value={})
    fleet = FleetOrchestrator(_FakeRouter({"dobby": primary, "rocky": worker}), primary="dobby")
    result = await fleet.automation_overview()
    assert result["actionableCounts"] == expected["actionableCounts"]
    assert result["statuses"]["autopilot"]["level"] == "unknown"
    primary.automation_overview.assert_awaited_once()
    worker.automation_overview.assert_not_awaited()
    worker.automation_statuses.assert_awaited_once()


# --- a lone DaemonTimeout is INFO; the probe's run decides escalation (#2821) ----


async def _unreachable_levels(caplog, exc, run):
    import logging
    from server.sessions import host_probe, host_upgrade

    fo = FleetOrchestrator(_FakeRouter({"dobby": _AggOrch("dobby")}), primary="dobby")

    async def not_expected(name, **_):
        return False

    host_probe._CONSECUTIVE_UNREACHABLE.pop("rocky", None)
    if run:
        host_probe._CONSECUTIVE_UNREACHABLE["rocky"] = run
    try:
        with mock.patch.object(host_upgrade, "restart_expected", not_expected), \
             caplog.at_level(logging.INFO, logger="server.sessions.fleet_orchestrator"):
            await fo._log_unreachable("rocky", exc, "list_sessions")
    finally:
        host_probe._CONSECUTIVE_UNREACHABLE.pop("rocky", None)
    return [r.levelno for r in caplog.records if "rocky" in r.getMessage()
            or getattr(r, "fields", {}).get("host") == "rocky"]


@pytest.mark.asyncio
async def test_lone_daemon_timeout_logs_info(caplog):
    import logging
    from server.ipc.client import DaemonTimeout
    levels = await _unreachable_levels(caplog, DaemonTimeout("list_sessions timed out:"), 0)
    assert levels == [logging.INFO]


@pytest.mark.asyncio
async def test_daemon_timeout_warns_once_probe_run_escalates(caplog):
    import logging
    from server.ipc.client import DaemonTimeout
    from server.sessions.host_probe import UNREACHABLE_ESCALATE_AFTER
    levels = await _unreachable_levels(
        caplog, DaemonTimeout("list_sessions timed out:"), UNREACHABLE_ESCALATE_AFTER)
    assert levels == [logging.WARNING]


@pytest.mark.asyncio
async def test_lone_local_deadline_timeout_logs_info(caplog):
    # automation_statuses wraps its worker read in asyncio.timeout(), which raises
    # the builtin TimeoutError, not DaemonTimeout — same condition, same policy (#2846).
    import logging
    levels = await _unreachable_levels(caplog, TimeoutError(), 0)
    assert levels == [logging.INFO]


@pytest.mark.asyncio
async def test_local_deadline_timeout_warns_once_probe_run_escalates(caplog):
    import logging
    from server.sessions.host_probe import UNREACHABLE_ESCALATE_AFTER
    levels = await _unreachable_levels(caplog, TimeoutError(), UNREACHABLE_ESCALATE_AFTER)
    assert levels == [logging.WARNING]


@pytest.mark.asyncio
async def test_non_daemon_error_warns_despite_empty_run(caplog):
    import logging
    levels = await _unreachable_levels(caplog, RuntimeError("boom"), 0)
    assert levels == [logging.WARNING]


def test_probe_mcp_merge_concatenates_rows_and_skipped():
    from server.sessions.fleet_orchestrator import _MERGE_ALL_HOSTS
    assert _MERGE_ALL_HOSTS["probe_mcp"]([
        {"rows": [1], "skipped": []}, {"rows": [2], "skipped": [{"account": "b"}]}, {},
    ]) == {"rows": [1, 2], "skipped": [{"account": "b"}]}


@pytest.mark.asyncio
async def test_x11_metrics_passes_daemon_native_display_through_from_primary():
    """/api/x11/metrics in fleet mode reaches the primary daemon's x11_metrics via
    __getattr__ untouched, so its nativeDisplay key survives."""
    class _Prim(_FakeOrch):
        async def x11_metrics(self):
            return {"enabled": True, "nativeDisplay": {"available": False, "reason": "cookie_missing"}}
    fo = FleetOrchestrator(_FakeRouter({"rocky": _Prim("rocky")}), primary="rocky")
    assert (await fo.x11_metrics())["nativeDisplay"]["reason"] == "cookie_missing"
