"""The draft-PR guard's ownership check: a session opened a PR when its own transcript
holds the successful ``gh pr create`` for it, or the PR's head is the session's own
worktree branch — never for a PR Doctor session spawned onto someone else's PR."""
import pytest

from server.sessions.events import ToolCall, ToolKind, ToolResult
from server.sessions.replay import ReplayWindow


def _orch(*, origin=None, branch="vp/abc123", repo="o/r", windows=None):
    import server.sessions.orchestrator as orch_mod
    from server.sessions.engine import AgentEngine
    from server.sessions.types import SessionRecord
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = {"s1": SessionRecord(
        id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
        engine_session_id="", cols=80, rows=24, cwd="/tmp", status="in_progress",
        repo=repo, branch=branch, origin=origin)}
    calls: list = []

    async def transcript_window(sid, **kw):
        calls.append(kw.get("limit"))
        return windows.pop(0) if windows else None
    orch.transcript_window = transcript_window
    orch.calls = calls
    return orch


def _created(url, total=3):
    return ReplayWindow(events=[
        ToolCall("t", "c", "Bash", ToolKind.BASH, {"command": "gh pr create --draft"}),
        ToolResult("c", True, {"content": url})], earliest_turn=0, total_turns=total)


@pytest.mark.asyncio
async def test_a_pr_the_session_created_is_its_own_in_any_repo():
    orch = _orch(windows=[_created("https://github.com/Other/Repo/pull/7")])
    assert await orch._session_opened_pr("s1", "other/repo", 7, "feat/elsewhere")


@pytest.mark.asyncio
async def test_a_dispatched_pr_doctor_session_does_not_own_the_pr_it_fixes():
    orch = _orch(origin="pr-doctor", branch="feat/spec",
                 windows=[ReplayWindow(events=[], earliest_turn=0, total_turns=1)])
    assert not await orch._session_opened_pr("s1", "o/r", 17, "feat/spec")


@pytest.mark.asyncio
async def test_head_on_the_sessions_own_branch_is_its_own():
    orch = _orch(windows=[])  # replay unavailable: the branch still answers
    assert await orch._session_opened_pr("s1", "O/R", 17, "vp/abc123")


@pytest.mark.asyncio
@pytest.mark.parametrize("repo, head, branch", [
    ("o/r", "feat/someone-else", "vp/abc123"),
    ("x/y", "vp/abc123", "vp/abc123"),      # same branch name, other repo
    ("o/r", "main", "main"),                # a session sitting on the default branch
])
async def test_other_prs_are_not_its_own(repo, head, branch):
    orch = _orch(branch=branch, windows=[_created("https://github.com/o/r/pull/9")])
    assert not await orch._session_opened_pr("s1", repo, 17, head)


@pytest.mark.asyncio
async def test_creates_are_read_incrementally_and_a_failed_replay_is_retried():
    orch = _orch(branch=None, windows=[
        None,                                                 # first replay fails
        ReplayWindow(events=[], earliest_turn=0, total_turns=4),   # full baseline
        ReplayWindow(events=[], earliest_turn=3, total_turns=6),   # tail probe
        _created("https://github.com/o/r/pull/17", total=6),       # the new suffix
    ])
    assert not await orch._session_opened_pr("s1", "o/r", 17, "x")
    assert not await orch._session_opened_pr("s1", "o/r", 17, "x")
    assert await orch._session_opened_pr("s1", "o/r", 17, "x")
    assert orch.calls == [None, None, 1, 3]


@pytest.mark.asyncio
async def test_unknown_session_owns_nothing():
    orch = _orch()
    assert not await orch._session_opened_pr("nope", "o/r", 1, "vp/abc123")


def test_session_factory_binds_owns_pr_only_for_a_factory_that_declares_it():
    orch = _orch()
    orch._overlays = {}
    orch._display_env = lambda sid: {}
    orch._oat_env = lambda sid: {}
    seen = {}

    def real_like(cwd, cfg, cut, mode, resume, *, on_pre_compact=None, owns_pr=None):
        seen["owns_pr"] = owns_pr

    def fake(cwd, cfg, cut, mode, resume, *, on_pre_compact=None):
        seen["fake"] = True

    orch._sdk_factory = real_like
    orch._session_factory("s1")("/tmp", "/cfg", None, "default", None)
    assert seen["owns_pr"].func == orch._session_opened_pr
    assert seen["owns_pr"].args == ("s1",)
    def with_grants(cwd, cfg, cut, mode, resume, *, on_pre_compact=None, owns_pr=None,
                    ready_grants=None):
        seen["ready_grants"] = ready_grants

    orch._sdk_factory = with_grants
    orch._session_factory("s1")("/tmp", "/cfg", None, "default", None)
    assert seen["ready_grants"].session_id == "s1"
    orch._sdk_factory = fake
    orch._session_factory("s1")("/tmp", "/cfg", None, "default", None)
    assert seen["fake"]
