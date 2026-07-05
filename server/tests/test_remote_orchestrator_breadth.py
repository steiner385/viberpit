import ast
from pathlib import Path
from unittest import mock

import pytest
import server
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.sessions.replay import ReplayWindow
from server.sessions.types import SessionRecord
from server.tests.ipc_harness import serve_daemon_uds


# Request-handling layers that run in the web (edge) process. In daemon mode the
# `orch` they call is a RemoteOrchestrator RPC proxy, NOT the real SessionOrchestrator,
# so every method they invoke must be explicitly proxied or it raises AttributeError
# -> 500 in prod (the #295 `request_pr_fix` incident, 2026-06-25).
_REQUEST_LAYER_DIRS = ("api", "bridge", "auth")

# Names bound to an orchestrator at these call sites, by convention.
_ORCH_VARS = {"orch", "orchestrator"}

# Attribute accesses that are intentionally NOT on RemoteOrchestrator (e.g. a path
# guarded to local mode only). Keep empty unless a deliberate exclusion is documented
# here — a bare AttributeError in daemon mode is a 500, not a graceful fallback.
_LOCAL_ONLY: set[str] = set()


def _orch_attr_accesses() -> set[str]:
    """AST-scan the request-handling layers for `orch.<x>` / `orchestrator.<x>` access."""
    root = Path(server.__file__).parent
    names: set[str] = set()
    for sub in _REQUEST_LAYER_DIRS:
        for path in (root / sub).rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in _ORCH_VARS
                    and not node.attr.startswith("_")
                ):
                    names.add(node.attr)
    return names


def test_remote_orchestrator_covers_every_request_layer_call():
    """Regression guard for #295: a method the API calls on `orch` but the RemoteOrchestrator
    proxy lacks AttributeErrors -> 500 in daemon mode, invisible to tsc/mypy (duck-typed).

    Scans the real source of the request-handling layers so a NEW call site is auto-covered;
    forces the author to either proxy the method or document a _LOCAL_ONLY exclusion."""
    called = _orch_attr_accesses() - _LOCAL_ONLY
    assert called, "scan found no orch.* call sites — selector likely broke"
    missing = sorted(
        name for name in called if not callable(getattr(RemoteOrchestrator, name, None))
    )
    assert not missing, (
        f"RemoteOrchestrator is missing {missing} — the request layer calls these on `orch`, "
        f"but daemon mode would AttributeError -> 500. Add a proxy method in server/sessions/remote.py "
        f"(or add to _LOCAL_ONLY if truly local-only)."
    )


def _rec(sid="s1"):
    return SessionRecord(id=sid, name="S", account="a", claude_session_id="cs",
                         cols=80, rows=24, cwd="/tmp")


def _build():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    register_orchestrator_rpc(app, orch)
    return app, orch


@pytest.mark.asyncio
async def test_simple_and_quirk_methods_round_trip():
    app, orch = _build()
    orch.permanent_delete = mock.AsyncMock(return_value=True)
    orch.resolve_permission = mock.MagicMock(return_value=False)
    orch.answer_question = mock.MagicMock(return_value=True)
    orch.rename_session = mock.AsyncMock(return_value=None)
    orch.list_archived = mock.AsyncMock(return_value=[{"id": "old"}])
    orch.unarchive = mock.AsyncMock(return_value=_rec("u1"))
    orch.adopt_session = mock.AsyncMock(return_value=(_rec("a1"), {"resumed": True, "terminatedPid": 7}))

    class _Ev:
        def to_wire(self): return {"type": "tick"}
    orch.transcript_events = mock.AsyncMock(return_value=[_Ev()])

    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            assert await ro.permanent_delete("s1") is True
            assert await ro.resolve_permission("s1", "p", "deny", "me") is False
            assert await ro.answer_question("s1", "q", ["opt"], "me") is True
            assert await ro.rename_session("s1", "New") is None
            assert await ro.list_archived() == [{"id": "old"}]
            un = await ro.unarchive("u1")
            assert un.id == "u1"
            rec, meta = await ro.adopt_session("cs", "me")
            assert rec.id == "a1" and meta == {"resumed": True, "terminatedPid": 7}
            assert await ro.transcript_events("s1") == [{"type": "tick"}]
        finally:
            await ro.aclose()
    orch.resolve_permission.assert_called_once_with(session_id="s1", perm_id="p", decision="deny", identity="me")


@pytest.mark.asyncio
async def test_transcript_window_returns_replay_window():
    """transcript_window resolves path via daemon RPC then replays locally."""
    app, orch = _build()
    orch.transcript_path = mock.MagicMock(return_value="/fake/t.jsonl")
    fake_window = ReplayWindow(events=[{"type": "textDelta", "text": "hi"}], earliest_turn=0, total_turns=1)

    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            with mock.patch("server.sessions.replay.replay_window", new=mock.AsyncMock(return_value=fake_window)) as patched:
                result = await ro.transcript_window("s1", limit=5)
            patched.assert_called_once_with("/fake/t.jsonl", before=None, limit=5)
            assert isinstance(result, ReplayWindow)
            assert result.total_turns == 1
        finally:
            await ro.aclose()


@pytest.mark.asyncio
async def test_transcript_window_no_path_returns_none():
    """transcript_window returns None when the daemon reports no transcript path."""
    app, orch = _build()
    # The resolve_transcript_path RPC calls _resolve_transcript_path (DB-glob
    # fallback included); mock that seam so no DB is needed here.
    orch._resolve_transcript_path = mock.AsyncMock(return_value=None)

    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            result = await ro.transcript_window("s1")
            assert result is None
        finally:
            await ro.aclose()


@pytest.mark.asyncio
async def test_transcript_window_replay_error_returns_none():
    """transcript_window returns None (no exception) when replay_window raises."""
    app, orch = _build()
    orch.transcript_path = mock.MagicMock(return_value="/fake/corrupt.jsonl")

    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            with mock.patch("server.sessions.replay.replay_window", new=mock.AsyncMock(side_effect=ValueError("corrupt"))):
                result = await ro.transcript_window("s1")
            assert result is None
        finally:
            await ro.aclose()
