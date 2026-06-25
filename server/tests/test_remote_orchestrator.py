import asyncio
from unittest import mock
import pytest
from server.ipc.client import DaemonClient, DaemonUnavailable
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
