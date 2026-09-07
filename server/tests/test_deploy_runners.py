"""Per-runner deploy lifecycle (spec §2). The ssh argv sequence IS the contract."""
from __future__ import annotations
import asyncio
import subprocess
from server.deploy import runners as R

SSH = ["ssh", "-oBatchMode=yes", "-oConnectTimeout=10", "rocky"]
ROCKY = R.RunnerTarget(name="rocky", endpoint="https://rocky:9443", enabled=True, healthy=True,
                       ssh_host="rocky", repo_path="/home/tony/GitHub/ccswitch")
DOBBY = R.RunnerTarget(name="dobby", endpoint="https://dobby:9443", enabled=True, healthy=True,
                       ssh_host=None, repo_path=None)


def _recorder(calls, fail_on=None):
    def run(argv, **kw):
        if "show" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="LoadState=loaded\n", stderr="")
        calls.append(list(argv))
        rc = 1 if (fail_on and any(fail_on in a for a in argv)) else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="boom")
    return run


class FakeClient:
    def __init__(self, calls):
        self.calls = calls
    async def call(self, method, **kw):
        self.calls.append(method)
        if method == "drain_status":
            return {"state": "ready"}
        if method == "restore_deploy":
            return {"restored": ["s1"], "failed": []}
        return {}
    async def aclose(self):
        pass


def _info(sha):
    async def fetch(endpoint):
        return {"sha": sha, "checkoutSha": sha}
    return fetch


def test_remote_update_commands_ff_only_and_conditional_pip():
    with_pip = R.remote_update_commands("/srv/cc", "abc1234", deps_changed=True)
    assert [s for s, _ in with_pip] == ["fetch", "merge", "pip", "sync"]
    assert with_pip[1][1] == ["git", "-C", "/srv/cc", "merge", "--ff-only", "abc1234"]
    assert with_pip[2][1] == ["/srv/cc/.venv/bin/python", "-m", "pip", "install", "-e",
                              "'/srv/cc[server]'", "--quiet"]
    assert [s for s, _ in R.remote_update_commands("/srv/cc", "abc1234", deps_changed=False)] == \
        ["fetch", "merge", "sync"]
    assert not any("reset" in a for _, argv in with_pip for a in argv)
    # host_upgrade.default_target_sha now hands RunnerDeploy the FULL 40-char sha (a
    # short one is only minimal-unique on the edge that abbreviated it) — the controller
    # passes the same shape via git.current_sha. Pin that it reaches the merge argv whole.
    full = "0d15f1280d8b1b9f296e60939c21ccd63935da63"
    assert R.remote_update_commands("/srv/cc", full, deps_changed=False)[1][1] == \
        ["git", "-C", "/srv/cc", "merge", "--ff-only", full]


def test_remote_rewind_uses_reset_hard():
    cmds = R.remote_rewind_commands("/srv/cc", "01d0000")
    assert cmds[1][1] == ["git", "-C", "/srv/cc", "reset", "--hard", "01d0000"]
    assert [s for s, _ in cmds] == ["fetch", "reset", "sync"]


def test_remote_rewind_accepts_the_full_sha_the_controller_actually_passes():
    """Coupling guard (restored from the deleted test_deploy_steps_rollback.py test): the
    controller rolls back to `git.current_sha` (`rev-parse @`, a full 40-char sha), and
    the rewind path VALIDATES that value — so a non-hex sha would turn a rollback into a
    raise, losing the rollback entirely. Pin the shape the caller really supplies."""
    full = "0d15f1280d8b1b9f296e60939c21ccd63935da63"
    cmds = R.remote_rewind_commands("/srv/cc", full)
    assert cmds[1][1] == ["git", "-C", "/srv/cc", "reset", "--hard", full]


def test_unsafe_values_are_refused_before_any_command():
    import pytest
    bad = R.RunnerTarget(name="x", endpoint="https://x", enabled=True, healthy=True,
                         ssh_host="-oProxyCommand=evil", repo_path="/srv/cc")
    calls: list = []
    rd = R.RunnerDeploy(bad, local_name="dobby", run=_recorder(calls), client_factory=lambda e: FakeClient([]),
                        fetch_info=_info("abc1234"))
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=False,
                               local_daemon_enabled=True))
    assert st.state == "failed" and "ssh_host" in (st.error or "")
    assert calls == []
    with pytest.raises(ValueError):
        R.remote_update_commands("/srv/cc", "not a sha", deps_changed=False)
    with pytest.raises(ValueError):
        R.remote_rewind_commands("/srv/cc", "not a sha")

    rewind_calls: list = []
    rd2 = R.RunnerDeploy(bad, local_name="dobby", run=_recorder(rewind_calls),
                         client_factory=lambda e: FakeClient([]), fetch_info=_info("01d0000"))
    asyncio.run(rd2.rewind("01d0000", daemon_changed=True))
    assert rd2.progress.state == "failed" and "ssh_host" in (rd2.progress.error or "")
    assert rewind_calls == []


def test_remote_daemon_changing_deploy_full_sequence():
    calls: list = []
    rpc: list = []
    rd = R.RunnerDeploy(ROCKY, local_name="dobby", run=_recorder(calls),
                        client_factory=lambda e: FakeClient(rpc), fetch_info=_info("abc1234"),
                        verify_interval_s=0)
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=True,
                               local_daemon_enabled=True))
    assert st.state == "done" and st.error is None and st.sha == "abc1234" and st.moved
    assert calls == [
        SSH + ["git", "-C", "/home/tony/GitHub/ccswitch", "fetch", "--quiet", "origin"],
        SSH + ["git", "-C", "/home/tony/GitHub/ccswitch", "merge", "--ff-only", "abc1234"],
        SSH + ["/home/tony/GitHub/ccswitch/.venv/bin/python", "-m", "pip", "install", "-e",
               "'/home/tony/GitHub/ccswitch[server]'", "--quiet"],
        SSH + ["sync"],
        SSH + ["sudo", "-n", "systemctl", "restart", "viberpit-daemon.service"],
    ]
    # snapshot before the update, drain + close_events before the restart, restore after verify
    assert rpc == ["snapshot_deploy", "begin_drain", "drain_status", "close_events_connections",
                   "restore_deploy"]
    assert st.restore == {"restored": ["s1"], "failed": []}


def test_remote_edge_only_deploy_updates_checkout_without_restart():
    calls: list = []
    rpc: list = []
    rd = R.RunnerDeploy(ROCKY, local_name="dobby", run=_recorder(calls),
                        client_factory=lambda e: FakeClient(rpc),
                        fetch_info=_info("abc1234"), verify_interval_s=0)
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=False, deps_changed=False,
                               local_daemon_enabled=True))
    assert st.state == "done"
    assert [c[-1] for c in calls] == ["origin", "abc1234", "sync"]
    assert "begin_drain" not in rpc and "restore_deploy" not in rpc
    assert not any("systemctl" in c for c in calls)


def test_edge_only_verify_accepts_old_process_sha_with_new_checkout():
    async def fetch(endpoint):
        return {"sha": "old0000", "checkoutSha": "abc1234"}
    rd = R.RunnerDeploy(ROCKY, local_name="dobby", run=_recorder([]),
                        client_factory=lambda e: FakeClient([]), fetch_info=fetch, verify_interval_s=0)
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=False, deps_changed=False,
                               local_daemon_enabled=True))
    assert st.state == "done"
    assert st.sha == "abc1234"


def test_daemon_changing_verify_requires_process_sha():
    async def fetch(endpoint):
        return {"sha": "old0000", "checkoutSha": "abc1234"}  # restarted into stale code
    rd = R.RunnerDeploy(ROCKY, local_name="dobby", run=_recorder([]),
                        client_factory=lambda e: FakeClient([]), fetch_info=fetch,
                        verify_timeout_s=0.05, verify_interval_s=0.01)
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=False,
                               local_daemon_enabled=True))
    assert st.state == "failed" and st.step == "verify"


def test_local_runner_restarts_locally_and_never_touches_the_checkout():
    calls: list = []
    rpc: list = []
    rd = R.RunnerDeploy(DOBBY, local_name="dobby", run=_recorder(calls),
                        client_factory=lambda e: FakeClient(rpc), fetch_info=_info("abc1234"),
                        verify_interval_s=0)
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=True,
                               local_daemon_enabled=True))
    assert st.state == "done" and st.moved is False
    assert calls == [["sudo", "systemctl", "restart", "viberpit-daemon.service"]]
    assert "begin_drain" in rpc


def test_local_runner_with_daemon_unit_disabled_is_skipped_not_failed():
    calls: list = []
    rd = R.RunnerDeploy(DOBBY, local_name="dobby", run=_recorder(calls),
                        client_factory=lambda e: FakeClient([]), fetch_info=_info("abc1234"))
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=False,
                               local_daemon_enabled=False))
    assert st.state == "disabled" and calls == []


def test_remote_without_ssh_config_is_unconfigured():
    t = R.RunnerTarget(name="ghost", endpoint="https://ghost:9443", enabled=True, healthy=True,
                       ssh_host=None, repo_path=None)
    rd = R.RunnerDeploy(t, local_name="dobby", run=_recorder([]), client_factory=lambda e: FakeClient([]),
                        fetch_info=_info("abc1234"))
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=False,
                               local_daemon_enabled=True))
    assert st.state == "unconfigured" and "ssh_host" in (st.error or "")


def test_offline_and_disabled_targets_are_skipped_by_deploy_runners():
    off = R.RunnerTarget(name="off", endpoint="https://off", enabled=True, healthy=False,
                         ssh_host="off", repo_path="/srv/cc",
                         last_error="ConnectError: [Errno 111] Connection refused")
    unk = R.RunnerTarget(name="unk", endpoint="https://unk", enabled=True, healthy=None,
                         ssh_host="unk", repo_path="/srv/cc")
    dis = R.RunnerTarget(name="dis", endpoint="https://dis", enabled=False, healthy=True,
                         ssh_host="dis", repo_path="/srv/cc")
    calls: list = []
    res = asyncio.run(R.deploy_runners(
        [off, unk, dis], target_sha="abc1234", daemon_changed=True, deps_changed=False,
        local_name="dobby", local_daemon_enabled=True, run=_recorder(calls),
        client_factory=lambda e: FakeClient([]), fetch_info=_info("abc1234")))
    assert {n: p.state for n, p in res.items()} == {"off": "offline", "unk": "offline", "dis": "disabled"}
    assert calls == []


def _fenced(name, last_error):
    return R.RunnerTarget(name=name, endpoint=f"https://{name}", enabled=True, healthy=False,
                          ssh_host="rocky", repo_path="/home/tony/GitHub/ccswitch",
                          last_error=last_error)


def test_skew_fenced_runner_is_a_deploy_target():
    """healthy=False does NOT mean unreachable. The probe fences a runner that answered
    fine but is on the wrong commit — and a deploy is precisely what un-fences it. Treating
    it as `offline` meant one failed deploy skipped that host from every LATER deploy while
    still reporting green, so it stayed fenced (and out of dispatch) forever."""
    calls: list = []
    rpc: list = []
    t = _fenced("rocky", "version skew: checkout=old0000 process=old0000 deployed=abc1234")
    rd = R.RunnerDeploy(t, local_name="dobby", run=_recorder(calls),
                        client_factory=lambda e: FakeClient(rpc), fetch_info=_info("abc1234"),
                        verify_interval_s=0)
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=False,
                               local_daemon_enabled=True))
    assert st.state == "done" and st.moved
    assert [c[-1] for c in calls] == ["origin", "abc1234", "sync", "viberpit-daemon.service"]
    assert rpc == ["snapshot_deploy", "begin_drain", "drain_status", "close_events_connections",
                   "restore_deploy"]


def test_protocol_mismatched_runner_is_a_deploy_target():
    """Same shape as the skew gate: reachable, fenced, and fixed by deploying to it."""
    calls: list = []
    t = _fenced("rocky", "protocol mismatch: daemon=1 edge=2")
    rd = R.RunnerDeploy(t, local_name="dobby", run=_recorder(calls),
                        client_factory=lambda e: FakeClient([]), fetch_info=_info("abc1234"),
                        verify_interval_s=0)
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=False,
                               local_daemon_enabled=True))
    assert st.state == "done"
    assert any("systemctl" in c for c in (a for call in calls for a in call))


def test_is_deployable_matches_the_probes_own_fenced_prefixes():
    """The predicate reads a string the PROBE writes; pin it against the probe's constant
    rather than against re-typed literals, so a reworded gate message cannot silently drop
    a host out of every deploy."""
    from server.sessions.host_probe import FENCED_REACHABLE_PREFIXES
    for prefix in FENCED_REACHABLE_PREFIXES:
        assert R.is_deployable(_fenced("h", f"{prefix}: details here"))
    assert not R.is_deployable(_fenced("h", "ConnectError: refused"))
    assert not R.is_deployable(_fenced("h", None))
    assert not R.is_deployable(R.RunnerTarget(
        name="h", endpoint="https://h", enabled=False, healthy=True, ssh_host="h",
        repo_path="/srv/cc", last_error=None)), "disabled is never deployable"


def test_failed_runner_does_not_stop_the_others_and_concurrency_is_capped():
    hosts = [R.RunnerTarget(name=f"h{i}", endpoint=f"https://h{i}", enabled=True, healthy=True,
                            ssh_host=f"h{i}", repo_path="/srv/cc") for i in range(4)]
    in_flight = [0]; peak = [0]

    async def fetch(endpoint):
        return {"sha": "abc1234", "checkoutSha": "abc1234"}

    def run(argv, **kw):
        # every remote step of h2 fails at merge; the others succeed
        rc = 1 if ("h2" in argv and "--ff-only" in argv) else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="boom")

    class SlowClient(FakeClient):
        async def call(self, method, **kw):
            if method == "begin_drain":
                in_flight[0] += 1; peak[0] = max(peak[0], in_flight[0])
                await asyncio.sleep(0.02)
                in_flight[0] -= 1
            return await super().call(method, **kw)

    res = asyncio.run(R.deploy_runners(
        hosts, target_sha="abc1234", daemon_changed=True, deps_changed=False, local_name="dobby",
        local_daemon_enabled=True, concurrency=2, run=run, client_factory=lambda e: SlowClient([]),
        fetch_info=fetch, verify_interval_s=0))
    assert res["h2"].state == "failed" and res["h2"].step == "merge"
    assert all(res[n].state == "done" for n in ("h0", "h1", "h3"))
    assert peak[0] <= 2


def test_rewind_only_moved_hosts_and_uses_reset_hard():
    """A daemon-changing deploy restarted the runner, so the rewind must too — and it must
    DRAIN first. A bare restart takes in-flight turns down with it, which is the whole
    reason the forward path drains; a rollback is not a licence to skip it."""
    timeline: list = []

    def run(argv, **kw):
        timeline.append(("run", " ".join(argv)))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    class TimelineClient(FakeClient):
        async def call(self, method, **kw):
            timeline.append(("rpc", method))
            return await super().call(method, **kw)

    res = {"rocky": R.HostProgress(state="done", moved=True),
           "other": R.HostProgress(state="failed", moved=False)}
    other = R.RunnerTarget(name="other", endpoint="https://o", enabled=True, healthy=True,
                           ssh_host="other", repo_path="/srv/cc")
    asyncio.run(R.rewind_runners([ROCKY, other], res, sha="01d0000", local_name="dobby",
                                 daemon_changed=True,
                                 run=run, client_factory=lambda e: TimelineClient([]),
                                 fetch_info=_info("01d0000"), verify_interval_s=0))
    joined = [c for k, c in timeline if k == "run"]
    assert any("reset --hard 01d0000" in j and "rocky" in j for j in joined)
    assert not any("other" in j for j in joined)
    assert joined[-1].endswith("sudo -n systemctl restart viberpit-daemon.service")
    # Shared timeline: only this proves the drain happens BEFORE the restart, not merely
    # that both happened (the ssh argv and the RPC list are asserted separately elsewhere).
    restart = next(i for i, e in enumerate(timeline)
                   if e[0] == "run" and "systemctl restart" in e[1])
    assert ("rpc", "begin_drain") in timeline
    assert timeline.index(("rpc", "begin_drain")) < restart
    assert timeline.index(("rpc", "close_events_connections")) < restart


def test_rewind_edge_only_resets_without_restart_or_drain():
    """An EDGE-ONLY deploy never restarted the runner, so its process is already on the
    rollback sha. The checkout still has to come back (the deploy advanced it on every
    host), but draining and restarting here would kill live turns to reach a state that is
    already true."""
    calls: list = []
    rpc: list = []
    res = {"rocky": R.HostProgress(state="done", moved=True)}
    asyncio.run(R.rewind_runners([ROCKY], res, sha="01d0000", local_name="dobby",
                                 daemon_changed=False,
                                 run=_recorder(calls), client_factory=lambda e: FakeClient(rpc),
                                 fetch_info=_info("01d0000"), verify_interval_s=0))
    joined = [" ".join(c) for c in calls]
    assert any("reset --hard 01d0000" in j for j in joined), "the checkout still rewinds"
    assert not any("systemctl" in j for j in joined)
    assert "begin_drain" not in rpc


def test_rewind_edge_only_verifies_the_checkout_not_the_process():
    """Corollary of the above: with the daemon left running old code, /info's process sha
    stays whatever it was. Verifying it would time the rewind out and report a rollback
    failure for a rollback that worked."""
    async def fetch(endpoint):
        return {"sha": "stale00", "checkoutSha": "01d0000"}
    rd = R.RunnerDeploy(ROCKY, local_name="dobby", run=_recorder([]),
                        client_factory=lambda e: FakeClient([]), fetch_info=fetch,
                        verify_timeout_s=0.05, verify_interval_s=0.01)
    asyncio.run(rd.rewind("01d0000", daemon_changed=False))
    assert rd.progress.state == "done" and rd.progress.sha == "01d0000"


def test_progress_callback_sees_each_step():
    seen: list = []
    rd = R.RunnerDeploy(ROCKY, local_name="dobby", run=_recorder([]),
                        client_factory=lambda e: FakeClient([]), fetch_info=_info("abc1234"),
                        verify_interval_s=0, on_progress=lambda name, p: seen.append((name, p.state, p.step)))
    asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=False,
                          local_daemon_enabled=True))
    steps = [s for _, _, s in seen]
    for expected in ("snapshot", "fetch", "merge", "sync", "drain", "restart", "verify", "restore"):
        assert expected in steps
    assert seen[-1][1] == "done"


def test_raising_progress_callback_does_not_stop_other_runners():
    h0 = R.RunnerTarget(name="h0", endpoint="https://h0", enabled=True, healthy=True,
                        ssh_host="h0", repo_path="/srv/cc")
    h1 = R.RunnerTarget(name="h1", endpoint="https://h1", enabled=True, healthy=True,
                        ssh_host="h1", repo_path="/srv/cc")

    def blow_up(name, p):
        raise RuntimeError("boom")

    res = asyncio.run(R.deploy_runners(
        [h0, h1], target_sha="abc1234", daemon_changed=True, deps_changed=False,
        local_name="dobby", local_daemon_enabled=True, run=_recorder([]),
        client_factory=lambda e: FakeClient([]), fetch_info=_info("abc1234"),
        verify_interval_s=0, on_progress=blow_up))
    assert res["h0"].state == "done" and res["h1"].state == "done"


# --- daemon-client lifecycle (moved here from test_deploy_controller.py) --------------
# The controller used to build these clients itself and owned these guarantees; the RPCs
# now live in RunnerDeploy, so the guarantees have to live here too.


def test_every_runner_client_is_closed():
    """The factory builds a NEW client per call site (snapshot, drain, close_events,
    restore), each owning two httpx clients, their transports and — on TLS — an SSL
    context. Left unclosed they hold their sockets until GC: exactly the short-lived-caller
    leak DaemonClient.aclose exists to prevent, once per call site per deploy."""
    built: list = []
    closed: list = []

    class CountingClient(FakeClient):
        def __init__(self, calls):
            super().__init__(calls)
            built.append(self)
        async def aclose(self):
            closed.append(self)

    rd = R.RunnerDeploy(ROCKY, local_name="dobby", run=_recorder([]),
                        client_factory=lambda e: CountingClient([]),
                        fetch_info=_info("abc1234"), verify_interval_s=0)
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=False,
                               local_daemon_enabled=True))
    assert st.state == "done"
    assert len(built) == 4, "snapshot, drain, close_events and restore each build one"
    assert closed == built, (
        f"{len(built) - len(closed)} of {len(built)} runner daemon clients leaked")


def test_a_raising_aclose_does_not_fail_the_runner():
    """Teardown is bookkeeping. It runs inside deploy()'s try, so a raising aclose would
    mark the runner FAILED — and since the fan-out gates the deploy, fail the WHOLE deploy
    over a closed transport. The drain's teardown used to be unguarded, unlike _rpc's."""
    class RudeClient(FakeClient):
        async def aclose(self):
            raise RuntimeError("transport already gone")

    rd = R.RunnerDeploy(ROCKY, local_name="dobby", run=_recorder([]),
                        client_factory=lambda e: RudeClient([]),
                        fetch_info=_info("abc1234"), verify_interval_s=0)
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=False,
                               local_daemon_enabled=True))
    assert st.state == "done" and st.error is None


def test_a_client_without_aclose_is_tolerated():
    """client_factory is an injection point and test doubles are plain objects with only
    ``call``; a missing aclose must be a no-op, never an AttributeError."""
    class BareClient:
        async def call(self, method, **kw):
            if method == "drain_status":
                return {"state": "ready"}
            if method == "restore_deploy":
                return {"restored": [], "failed": []}
            return {}

    rd = R.RunnerDeploy(ROCKY, local_name="dobby", run=_recorder([]),
                        client_factory=lambda e: BareClient(),
                        fetch_info=_info("abc1234"), verify_interval_s=0)
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=False,
                               local_daemon_enabled=True))
    assert st.state == "done" and st.error is None


def test_close_events_precedes_the_restart():
    """#740: dropping the edge's /events WebSocket BEFORE the daemon gets SIGTERM lets
    uvicorn exit promptly instead of holding graceful shutdown open ~45s waiting for the
    edge to reconnect. The RPC list and the ssh argv are asserted in separate tests, so
    only a SHARED timeline can prove the ordering BETWEEN them."""
    timeline: list = []

    def run(argv, **kw):
        timeline.append(("run", argv[-1]))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    class TimelineClient(FakeClient):
        async def call(self, method, **kw):
            timeline.append(("rpc", method))
            return await super().call(method, **kw)

    rd = R.RunnerDeploy(ROCKY, local_name="dobby", run=run,
                        client_factory=lambda e: TimelineClient([]),
                        fetch_info=_info("abc1234"), verify_interval_s=0)
    st = asyncio.run(rd.deploy(target_sha="abc1234", daemon_changed=True, deps_changed=False,
                               local_daemon_enabled=True))
    assert st.state == "done"
    assert ("rpc", "close_events_connections") in timeline
    assert ("run", "viberpit-daemon.service") in timeline
    assert timeline.index(("rpc", "close_events_connections")) < \
        timeline.index(("run", "viberpit-daemon.service"))
