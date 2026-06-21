"""orchestrator.adopt_session — terminate original + resume in place, no worktree."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from server.db import init_db
from server.sessions.adopt import AdoptionCandidate
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


def _candidate(cwd: str, sid: str, pid=999, alive=True) -> AdoptionCandidate:
    return AdoptionCandidate(
        pid=pid, account="kindash", cwd=cwd, claude_session_id=sid,
        transcript_path="/x.jsonl", name="Adopted", mapped=True, tracked=False,
        skip_reason=None, last_activity=datetime.now(timezone.utc), pid_alive=alive,
    )


@pytest.mark.asyncio
async def test_adopt_resumes_in_place_no_worktree(tmp_path, monkeypatch):
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    cwd = tmp_path / "repo"
    cwd.mkdir()
    sid = "44444444-4444-4444-4444-444444444444"
    cand = _candidate(str(cwd), sid)
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), repo_root=str(tmp_path))
    monkeypatch.setattr(orch, "_resolve_candidate", lambda s: cand)
    monkeypatch.setattr("server.sessions.orchestrator.compose", lambda a: tmp_path / "cfg")
    monkeypatch.setattr("server.sessions.orchestrator.configdir.trust_project",
                        lambda *a, **k: None)

    terminated = []
    monkeypatch.setattr(
        "server.sessions.orchestrator._adopt.terminate_process_tree",
        lambda pid, killer: terminated.append(pid) or True,
    )
    rec, outcome = await orch.adopt_session(sid, identity="t@e.com")

    assert rec.cwd == str(cwd)              # original cwd, not a worktree
    assert rec.adopted_in_place is True
    assert outcome["terminatedPid"] == 999 and outcome["resumed"] is True
    assert terminated == [999]             # the original process was terminated
    assert rec.id not in orch._worktrees   # no worktree registered
    assert orch.get_session(rec.id) is rec


@pytest.mark.asyncio
async def test_adopt_dead_pid_skips_termination(tmp_path, monkeypatch):
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    cwd = tmp_path / "repo"
    cwd.mkdir()
    sid = "55555555-5555-5555-5555-555555555555"
    cand = _candidate(str(cwd), sid, pid=12345, alive=False)
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), repo_root=str(tmp_path))
    monkeypatch.setattr(orch, "_resolve_candidate", lambda s: cand)
    monkeypatch.setattr("server.sessions.orchestrator.compose", lambda a: tmp_path / "cfg")
    monkeypatch.setattr("server.sessions.orchestrator.configdir.trust_project",
                        lambda *a, **k: None)

    def _no_kill(pid, signum):  # pragma: no cover - must never be called
        raise AssertionError("dead pid must not be signalled")

    rec, outcome = await orch.adopt_session(sid, identity="t@e.com", killer=_no_kill)
    assert outcome["terminatedPid"] is None and outcome["resumed"] is True
    assert rec.adopted_in_place is True


@pytest.mark.asyncio
async def test_adopt_rejects_unmapped(monkeypatch):
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    monkeypatch.setattr(orch, "_resolve_candidate", lambda s: None)
    with pytest.raises(KeyError):
        await orch.adopt_session("gone", identity="t@e.com")


def test_terminate_dead_pid_is_noop():
    import signal

    from server.sessions.adopt import terminate_process_tree
    sent = []
    result = terminate_process_tree(
        42, killer=lambda p, s: sent.append((p, s)), alive=lambda p: False)
    assert result is False and sent == []
    # avoid unused import flake
    assert signal.SIGTERM


def test_terminate_sigterm_then_sigkill_when_stubborn():
    import signal

    from server.sessions.adopt import terminate_process_tree
    sent = []
    # alive() always True → SIGTERM, wait the (tiny) grace, then SIGKILL.
    result = terminate_process_tree(
        77, killer=lambda p, s: sent.append(s), alive=lambda p: True, grace=0.01)
    assert result is True
    assert sent == [signal.SIGTERM, signal.SIGKILL]


def test_terminate_clean_exit_skips_sigkill():
    import signal

    from server.sessions.adopt import terminate_process_tree
    state = {"alive": True}
    sent = []

    def killer(pid, signum):
        sent.append(signum)
        if signum == signal.SIGTERM:
            state["alive"] = False  # process exits cleanly on SIGTERM

    result = terminate_process_tree(
        88, killer=killer, alive=lambda p: state["alive"], grace=1.0)
    assert result is True and sent == [signal.SIGTERM]  # no SIGKILL needed
