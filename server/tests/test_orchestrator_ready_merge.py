"""`ready_merge` must surface gh's refusal text (roadmap auto-execution §6).

The SDLC synthesizer records `gate_refused` with GitHub's own message, so the
one shared merge path has to hand it back instead of swallowing it.
"""
import subprocess

import pytest

from server.sessions.orchestrator import SessionOrchestrator


def _proc(code: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=code, stdout="", stderr=stderr)


@pytest.mark.asyncio
async def test_ready_merge_reports_gh_refusal_text():
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    calls = []

    def fake_gh(args, *a, **kw):
        calls.append(args)
        if args[1] == "ready":
            return _proc(0)
        return _proc(1, "Auto-merge is not allowed for this repository")

    orch._gh_run = fake_gh
    out = await SessionOrchestrator.ready_merge(orch, "o/r", 7)

    assert out["autoMergeArmed"] is False
    assert out["error"] == "Auto-merge is not allowed for this repository"


@pytest.mark.asyncio
async def test_ready_merge_already_armed_is_success_with_no_error():
    orch = SessionOrchestrator.__new__(SessionOrchestrator)

    def fake_gh(args, *a, **kw):
        if args[1] == "ready":
            return _proc(1)          # already ready — not an error
        return _proc(1, "auto-merge is already enabled for this pull request")

    orch._gh_run = fake_gh
    out = await SessionOrchestrator.ready_merge(orch, "o/r", 7)

    assert out["autoMergeArmed"] is True
    assert out["alreadyArmed"] is True
    assert out["error"] == ""
