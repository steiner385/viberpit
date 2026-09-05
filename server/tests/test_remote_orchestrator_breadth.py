import ast
import inspect
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
from server.sessions.engine import AgentEngine
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


def _orch_attr_accesses(*, private: bool = False) -> set[str]:
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
                    and node.attr.startswith("_") == private
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


def _orch_call_kwargs() -> dict[str, set[str]]:
    """AST-scan the request-handling layers for `orch.<method>(..., kw=...)` call
    sites, returning {method_name: {kwarg names passed at any call site}}."""
    root = Path(server.__file__).parent
    calls: dict[str, set[str]] = {}
    for sub in _REQUEST_LAYER_DIRS:
        for path in (root / sub).rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in _ORCH_VARS
                ):
                    kwnames = {kw.arg for kw in node.keywords if kw.arg is not None}
                    calls.setdefault(node.func.attr, set()).update(kwnames)
    return calls


def test_remote_orchestrator_signatures_accept_the_kwargs_call_sites_pass():
    """Whole-branch review finding 1(d): the name-only guard above (and
    FleetOrchestrator.__getattr__'s blind **k forwarding) hide a SIGNATURE mismatch
    from mypy — RemoteOrchestrator.resolve_model_approval used to lack a `model`
    parameter entirely, so `orch.resolve_model_approval(..., model=body.model, ...)`
    in server/api/sessions.py raised TypeError -> 500 in daemon mode, invisible to
    every test that only calls the method with kwargs it happens to accept.

    For every orch.<method>(..., kw=...) call site found in the request layers,
    assert the RemoteOrchestrator method's signature actually accepts that keyword
    (or the method accepts **kwargs)."""
    calls = _orch_call_kwargs()
    assert calls, "AST scan found no keyword orch.* calls — selector likely broke"
    problems = []
    for name, kwargs in calls.items():
        method = getattr(RemoteOrchestrator, name, None)
        if not callable(method):
            continue  # already reported by test_remote_orchestrator_covers_every_request_layer_call
        sig = inspect.signature(method)
        params = sig.parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            continue
        missing = kwargs - set(params)
        if missing:
            problems.append(f"{name} missing {sorted(missing)}")
    assert not problems, (
        "RemoteOrchestrator method signature(s) don't accept keyword(s) the request "
        "layer passes at a real call site: " + "; ".join(problems) + " — this is a "
        "silent TypeError -> 500 in daemon mode (see #295 and the whole-branch "
        "finding-1 fix for resolve_model_approval)."
    )
    # At minimum, the specific seam this finding was about:
    assert "model" in inspect.signature(RemoteOrchestrator.resolve_model_approval).parameters


def test_request_layer_never_touches_private_orchestrator_attrs():
    """The guard above only checks PUBLIC names, so a private `orch._x` call site slips
    past it — and private methods are never proxied on RemoteOrchestrator by convention.
    That combination broke archived-session search: the API called a private
    orch transcript-path resolver that only the in-process facade had, so daemon mode
    AttributeError'd -> 500 and the frontend silently showed the unfiltered list."""
    private = sorted(_orch_attr_accesses(private=True))
    assert not private, (
        f"request layer accesses private orchestrator attrs {private} — these bypass the "
        f"public-coverage guard and are missing from RemoteOrchestrator in daemon mode. "
        f"Expose a public method on the orchestrator (+ facade + remote proxy) instead."
    )


def _rec(sid="s1"):
    return SessionRecord(id=sid, name="S", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="cs",
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
            assert await ro.list_archived() == [{
                "id": "old",
                "engine": "claude_code",
                "engineSessionId": None,
                "reasoningEffort": None,
            }]
            un = await ro.unarchive("u1")
            assert un.id == "u1"
            rec, meta = await ro.adopt_session("cs", "me")
            assert rec.id == "a1" and meta == {"resumed": True, "terminatedPid": 7}
            assert await ro.transcript_events("s1") == [{"type": "tick"}]
        finally:
            await ro.aclose()
    orch.resolve_permission.assert_called_once_with(session_id="s1", perm_id="p", decision="deny", identity="me")


@pytest.mark.asyncio
async def test_resolve_transcript_path_round_trips():
    """resolve_transcript_path must be proxied (archived-scope search calls it on the
    edge orchestrator to read transcripts for matching)."""
    app, orch = _build()
    orch.resolve_transcript_path = mock.AsyncMock(return_value="/fake/arch.jsonl")

    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            assert await ro.resolve_transcript_path("s1") == "/fake/arch.jsonl"
        finally:
            await ro.aclose()
    orch.resolve_transcript_path.assert_called_once_with(session_id="s1")


@pytest.mark.asyncio
async def test_transcript_window_returns_replay_window():
    """transcript_window is served end-to-end by the daemon: the transcript_window
    RPC handler calls the daemon-side orchestrator's transcript_window (which resolves
    the path AND replays it, both in-process on the daemon's own host), and
    RemoteOrchestrator only deserializes the already-built response — it never reads
    the transcript file itself, so this is correct even when the daemon and edge live
    on different hosts."""
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
    # The resolve_transcript_path RPC calls the orchestrator resolver (DB-glob
    # fallback included); mock that seam so no DB is needed here.
    orch.resolve_transcript_path = mock.AsyncMock(return_value=None)

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


# --- #709: cross-host inspection RPCs (discovery / archive move / clone check
#     all run on the daemon host, never the edge process) -------------------


@pytest.mark.asyncio
async def test_list_adoptable_candidates_round_trips():
    """The /adoptable endpoint calls orch.list_adoptable_candidates; discovery runs
    on the daemon's /proc and the wire dicts cross the RPC unchanged (#709 finding 4)."""
    app, orch = _build()
    wire = [{"pid": 5, "claudeSessionId": "aaa", "skipReason": None, "pidAlive": True}]
    orch.list_adoptable_candidates = mock.AsyncMock(return_value=wire)

    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            assert await ro.list_adoptable_candidates() == wire
        finally:
            await ro.aclose()
    orch.list_adoptable_candidates.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_migrate_archive_home_round_trips():
    """Settings save delegates the archive-dir moves to the daemon, which owns the
    filesystem the archives live on (#709 finding 5). Params + result round-trip."""
    app, orch = _build()
    orch.migrate_archive_home = mock.AsyncMock(return_value={"moved": 2, "failures": 0})

    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            result = await ro.migrate_archive_home(None, "/new/home", ["s1", "s2"])
            assert result == {"moved": 2, "failures": 0}
        finally:
            await ro.aclose()
    orch.migrate_archive_home.assert_awaited_once_with(
        old_home=None, new_home="/new/home", archived_ids=["s1", "s2"])


@pytest.mark.asyncio
async def test_cloned_repos_round_trips():
    """The repo picker's cloned badge asks the daemon which clones exist on its own
    filesystem (#709 finding 6); the edge never stats a path itself."""
    app, orch = _build()
    orch.cloned_repos = mock.AsyncMock(return_value=["o/present"])

    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            assert await ro.cloned_repos(["o/present", "o/absent"]) == ["o/present"]
        finally:
            await ro.aclose()
    orch.cloned_repos.assert_awaited_once_with(names=["o/present", "o/absent"])


# --- whole-branch review finding 1: resolve_model_approval's `model` counter-offer
#     must survive the RPC boundary in BOTH directions (remote.py's call, and the
#     daemon-side handler reading it back out of params) --------------------------


@pytest.mark.asyncio
async def test_resolve_model_approval_forwards_model_to_wire_call():
    """MANDATORY PROOF (finding 1): with the RPC client stubbed directly (no real
    daemon), the `model` counter-offer must reach the wire call — or the human's
    chosen model silently vanishes and the session gets what IT asked for instead
    of the model the human actually named."""
    client = mock.AsyncMock()
    client.call.return_value = {"session": _rec("s1").to_dict()}
    ro = RemoteOrchestrator(client)
    rec = await ro.resolve_model_approval("s1", "approve", model="opus", identity="me")
    assert rec.id == "s1"
    client.call.assert_awaited_once_with(
        "resolve_model_approval", session_id="s1", decision="approve",
        model="opus", identity="me")


@pytest.mark.asyncio
async def test_resolve_model_approval_round_trips_through_daemon():
    """End-to-end over a real (in-memory) daemon: the RPC handler must also read
    params["model"] back out, or the counter-offer vanishes at the daemon boundary
    even though the wire call above carries it correctly."""
    app, orch = _build()
    orch.resolve_model_approval = mock.AsyncMock(return_value=_rec("s1"))

    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            rec = await ro.resolve_model_approval("s1", "approve", model="opus", identity="me")
            assert rec.id == "s1"
        finally:
            await ro.aclose()
    orch.resolve_model_approval.assert_awaited_once_with(
        "s1", "approve", model="opus", identity="me")


@pytest.mark.asyncio
async def test_resolve_model_approval_omitted_model_still_works():
    """An older client (or a plain Approve/Deny with no counter-offer) omits `model`
    entirely — params.get("model") must default to None rather than KeyError, so the
    pre-existing fable approval gate (which predates the counter-offer feature)
    keeps working."""
    app, orch = _build()
    orch.resolve_model_approval = mock.AsyncMock(return_value=_rec("s1"))

    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            rec = await ro.resolve_model_approval("s1", "deny", identity="me")
            assert rec.id == "s1"
        finally:
            await ro.aclose()
    orch.resolve_model_approval.assert_awaited_once_with(
        "s1", "deny", model=None, identity="me")


@pytest.mark.asyncio
async def test_resolve_model_approval_reconstitutes_model_blocked_error():
    """A blocked counter-offer must surface as ModelBlockedError (-> 409) in remote
    mode too, not degrade to a bare DaemonError (-> 500)."""
    from server.sessions.orchestrator import ModelBlockedError

    app, orch = _build()

    async def _raise(*a, **k):
        raise ModelBlockedError("opus")
    orch.resolve_model_approval = _raise

    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            with pytest.raises(ModelBlockedError):
                await ro.resolve_model_approval("s1", "approve", model="opus", identity="me")
        finally:
            await ro.aclose()
