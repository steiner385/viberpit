import asyncio
import types
from unittest import mock
import pytest
from server.ipc.client import DaemonClient, DaemonError, DaemonUnavailable
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.sessions.types import SessionRecord
from server.tests.ipc_harness import serve_daemon_uds


def _rec(sid="s1"):
    return SessionRecord(id=sid, name="S1", account="acct", claude_session_id="cs",
                         cols=80, rows=24, cwd="/tmp")


class _FakeEvent:
    def to_wire(self): return {"type": "textDelta", "turnId": "t1", "text": "hi"}


class _FakeDriver:
    def current_status(self): return "idle"
    def queued(self): return []

    async def events(self):
        yield _FakeEvent()


def _build_app():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    register_orchestrator_rpc(app, orch)
    return app, orch


@pytest.mark.asyncio
async def test_pr_for_over_uds():
    """The edge resolves a session's PR through the daemon's poller cache."""
    from server.github.pr_status import PullRequestInfo
    app, orch = _build_app()
    cached = PullRequestInfo(9, "https://github.com/o/r/pull/9", "open", "passing", "t")
    orch._pr_poller._cache[("o/r", "br")] = cached
    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            got = await ro.pr_for("o/r", "br")
            missing = await ro.pr_for("o/r", "no-such-branch")
            no_args = await ro.pr_for(None, None)
        finally:
            await ro.aclose()
    assert got == cached
    assert missing is None
    assert no_args is None


@pytest.mark.asyncio
async def test_prs_for_over_uds():
    """The edge resolves a session's full PR list through the daemon."""
    from server.github.pr_status import PullRequestInfo
    from server.sessions.types import SessionRecord
    app, orch = _build_app()
    orch._records["s1"] = SessionRecord(
        id="s1", name="S1", account="acct", claude_session_id="cs", cols=80, rows=24,
        cwd="/tmp", repo="o/r", authored_branches=["feat/a", "feat/b"],
    )
    cache = {
        ("o/r", "feat/a"): PullRequestInfo(1, "u1", "open", "passing", "A"),
        ("o/r", "feat/b"): PullRequestInfo(2, "u2", "merged", "passing", "B"),
    }
    orch._pr_poller.cached = lambda repo, br: cache.get((repo, br))
    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            got = await ro.prs_for("s1")
            empty = await ro.prs_for("nope")
        finally:
            await ro.aclose()
    assert [p.number for p in got] == [1, 2]
    assert empty == []


@pytest.mark.asyncio
async def test_automation_spawn_rpcs_use_extended_timeout():
    """consolidate/trigger run the whole clone→model-pick→spawn pipeline inline
    in the daemon RPC and legitimately exceed the 30s client default, so the
    edge must pass an extended per-call timeout or it 503s a spawn the daemon
    actually completes (issue #567)."""
    from server.sessions.remote import AUTOMATION_RPC_TIMEOUT, RemoteOrchestrator

    calls: list[tuple[str, float | None]] = []

    class _RecordingClient:
        async def call(self, method, *, timeout=None, **params):
            calls.append((method, timeout))
            return {"status": "spawned"}

    ro = RemoteOrchestrator(_RecordingClient())
    await ro.automation_consolidate("o/r", [1, 2])
    await ro.automation_trigger("pr-doctor", "o/r#1", "ci_failure")

    assert AUTOMATION_RPC_TIMEOUT > 30.0
    by_method = dict(calls)
    assert by_method["automation_consolidate"] == AUTOMATION_RPC_TIMEOUT
    assert by_method["automation_trigger"] == AUTOMATION_RPC_TIMEOUT


@pytest.mark.asyncio
async def test_run_command_translates_daemon_keyerror_back_to_keyerror():
    """Unknown-slash-command parity across the IPC boundary (#164).

    In-process, orchestrator.run_command raises KeyError(name) for an unknown command
    and ws.py's `except KeyError` renders a clean "unknown command" result. But an
    exception's *type* doesn't survive the RPC round-trip — it collapses to
    DaemonError — so without translation the remote path fell through to the generic
    "command failed" branch. RemoteOrchestrator must reconstitute the KeyError so the
    remote surface behaves identically to the local one."""
    class _KeyErrClient:
        async def call(self, method, **params):
            raise DaemonError("KeyError", "'bogus'")

    ro = RemoteOrchestrator(_KeyErrClient())
    with pytest.raises(KeyError):
        await ro.run_command("s1", "bogus", "")


@pytest.mark.asyncio
async def test_run_command_passes_through_non_keyerror_daemon_errors():
    """A non-KeyError daemon failure must NOT be masquerade as an unknown command —
    it stays a DaemonError so ws.py reports the generic failure, matching the
    in-process path (where only KeyError means 'unknown command')."""
    class _BoomClient:
        async def call(self, method, **params):
            raise DaemonError("ValueError", "boom")

    ro = RemoteOrchestrator(_BoomClient())
    with pytest.raises(DaemonError):
        await ro.run_command("s1", "cmd", "")


@pytest.mark.asyncio
async def test_run_command_unknown_over_uds_surfaces_keyerror():
    """End-to-end: a real unknown command over the real daemon IPC surfaces a
    KeyError on the edge (the daemon envelope carries type=KeyError). This proves the
    whole path — daemon raise → error envelope → DaemonError → translation — so ws.py
    can render 'unknown command' in daemon mode exactly as it does in-process."""
    app, orch = _build_app()

    class _NoCmdDriver:
        async def server_info(self):
            return {}  # empty catalog → every non-synthetic command is unknown

    orch._drivers["s1"] = _NoCmdDriver()
    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            with pytest.raises(KeyError):
                await ro.run_command("s1", "totally-bogus-cmd-xyz", "")
            # A session with no live driver is NOT an error — it returns a clean
            # "not live" notice (return value crosses IPC losslessly, no exception).
            notice = await ro.run_command("no-such-session", "anything", "")
        finally:
            await ro.aclose()
    assert notice["render"] == "notice"
    assert "isn't live" in notice["data"]["text"]


@pytest.mark.asyncio
async def test_request_pr_fix_over_uds():
    """Manual 'Fix PR #N' tap routes through the daemon to the real orchestrator.

    Regression: RemoteOrchestrator had no request_pr_fix proxy, so the deployed
    (daemon-mode) /fix-pr endpoint 500'd with AttributeError.
    """
    from server.github.pr_status import PullRequestInfo
    app, orch = _build_app()
    orch._records["s1"] = SessionRecord(
        id="s1", name="S1", account="acct", claude_session_id="cs", cols=80, rows=24,
        cwd="/tmp", repo="o/r", authored_branches=["feat/a"],
    )
    orch._pr_poller.cached = lambda repo, br: (
        PullRequestInfo(7, "u7", "open", "passing", "T") if (repo, br) == ("o/r", "feat/a") else None
    )
    with mock.patch.object(orch, "send_prompt", new=mock.AsyncMock(return_value="sent")) as sp:
        async with serve_daemon_uds(app) as sock:
            ro = RemoteOrchestrator(DaemonClient(sock))
            try:
                sent = await ro.request_pr_fix("s1", 7)
                no_pr = await ro.request_pr_fix("s1", 999)
            finally:
                await ro.aclose()
    assert sent == "sent"
    assert no_pr == "no_pr"
    sp.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_get_create_send_over_uds():
    app, orch = _build_app()
    orch._records["s1"] = _rec("s1")
    with mock.patch.object(orch, "create_session",
                           new=mock.AsyncMock(return_value=_rec("new1"))), \
         mock.patch.object(orch, "send_prompt", new=mock.AsyncMock(return_value="sent")) as sp:
        async with serve_daemon_uds(app) as sock:
            ro = RemoteOrchestrator(DaemonClient(sock))
            try:
                listed = await ro.list_sessions()
                got = await ro.get_session("s1")
                missing = await ro.get_session("nope")
                created = await ro.create_session(name="S", account="acct")
                await ro.send_prompt("s1", "hello")
            finally:
                await ro.aclose()
    assert [r.id for r in listed] == ["s1"]
    assert got.id == "s1" and missing is None
    assert created.id == "new1"
    sp.assert_awaited_once_with("s1", "hello", None)


@pytest.mark.asyncio
async def test_ensure_driver_and_stream_over_uds():
    app, orch = _build_app()
    with mock.patch.object(orch, "ensure_driver",
                           new=mock.AsyncMock(return_value=_FakeDriver())):
        async with serve_daemon_uds(app) as sock:
            ro = RemoteOrchestrator(DaemonClient(sock))
            try:
                stream = ro.subscribe("s1")
                await ro._client.events_ready("s1")
                found = await ro.ensure_driver("s1")
                ev = await asyncio.wait_for(stream.__anext__(), 2)
            finally:
                await ro.aclose()
    # A2.2b-1: ensure_driver now returns a RemoteDriver (not a bool) so ws.py can
    # consume it like a local driver; truthy/non-None signals "found".
    from server.sessions.remote import RemoteDriver
    assert isinstance(found, RemoteDriver)
    assert ev == {"type": "textDelta", "turnId": "t1", "text": "hi"}


@pytest.mark.asyncio
async def test_daemon_unavailable_propagates():
    ro = RemoteOrchestrator(DaemonClient("/nonexistent/ccsd.sock"))
    with pytest.raises(DaemonUnavailable):
        await ro.list_sessions()
    await ro.aclose()


def test_remote_orchestrator_latest_summary_proxies_with_viewing():
    from unittest.mock import AsyncMock

    ro = RemoteOrchestrator.__new__(RemoteOrchestrator)
    ro._client = types.SimpleNamespace(call=AsyncMock(return_value="Doing X"))
    out = asyncio.run(ro.latest_summary("s1", viewing=True))
    assert out == "Doing X"
    ro._client.call.assert_awaited_once_with("latest_summary", session_id="s1", viewing=True)
