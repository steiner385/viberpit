"""Async manual pr-doctor trigger end-to-end (issue #668).

Drives the real orchestrator wiring — ``_pr_doctor_trigger_async`` →
``_pr_doctor_job_run`` → ``_pr_doctor_session_fix`` — with the clone / model /
spawn seams stubbed, asserting the job advances through its phases and settles.
The point of the feature is that the trigger returns a job id *immediately* and
the slow pipeline runs in the background, so we assert both.
"""
from __future__ import annotations
from server.tests.test_pr_doctor_step import github_identity as github_identity


import asyncio

import pytest

import viberpit.gitrepos as gitrepos
import server.automations.model_pick as model_pick
from server.github.pr_status import PullRequestInfo
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

# PR Doctor refuses untrusted/unmirrored PRs; these tests cover its other behaviour.
pytestmark = pytest.mark.usefixtures("pr_authors_trusted")


def _pr(**kw):
    base = dict(number=777, url="https://x/777", state="open", checks="failing",
                title="fix the thing", repo="o/r", head_ref="feat/x",
                merge_state_status="", is_draft=False, mergeable="UNKNOWN")
    base.update(kw)
    return PullRequestInfo(**base)


class _Rec:
    id = "sess-new"


def _orch():
    return SessionOrchestrator(sdk_factory=fake_sdk_factory())


@pytest.mark.asyncio
async def test_async_trigger_runs_phases_then_settles_ready(monkeypatch, isolated_db):
    from server.automations.registry import registry
    await registry.ensure_rows()
    orch = _orch()
    phases: list[str] = []

    async def _root():
        return "/tmp/root"
    monkeypatch.setattr(orch, "_effective_repo_root", _root)
    monkeypatch.setattr(gitrepos, "ensure_clone", lambda root, repo, url: "/tmp/clone")

    async def _resolve(cfg, prompt):
        return None, None
    monkeypatch.setattr(model_pick, "resolve_model", _resolve)

    async def _spawn(**kw):
        return _Rec()
    monkeypatch.setattr(orch._pr_doctor_ctx, "spawn_session", _spawn)

    async def _emit(*a, **k):
        return None
    monkeypatch.setattr(orch._pr_doctor_ctx, "emit", _emit)

    # Record the phase sequence the real session-fix reports.
    orig = orch._pr_doctor_jobs.advance
    def _spy(job, phase):
        phases.append(phase)
        orig(job, phase)
    monkeypatch.setattr(orch._pr_doctor_jobs, "advance", _spy)

    # rec=None → no live driver → the spawn path (clone → assess → spawn).
    job_id = await orch._pr_doctor_trigger_async("o/r", "feat/x", _pr(), "failing_ci", None)

    # Immediate answer: a job id, and it exists in queued/running state.
    assert job_id.startswith("prd-")
    assert orch.pr_doctor_job(job_id)["status"] in ("queued", "running")

    # Drain the background task, then assert the terminal state.
    await asyncio.gather(*list(orch._pr_doctor_job_tasks))

    assert phases == ["assessing", "spawning"]
    job = orch.pr_doctor_job(job_id)
    assert (job["status"], job["phase"], job["level"]) == ("ready", "ready", "ok")
    assert job["session_id"] == "sess-new"
    assert job["error_phase"] is None


@pytest.mark.asyncio
async def test_async_trigger_records_clone_failure_phase(monkeypatch, isolated_db):
    from server.automations.registry import registry
    await registry.ensure_rows()
    orch = _orch()

    async def _root():
        return "/tmp/root"
    monkeypatch.setattr(orch, "_effective_repo_root", _root)

    def _boom(root, repo, url):
        raise gitrepos.CloneError("network down")
    async def failed_spawn(**kwargs):
        raise gitrepos.CloneError("network down")
    monkeypatch.setattr(orch._pr_doctor_ctx, "spawn_session", failed_spawn)

    async def _emit(*a, **k):
        return None
    monkeypatch.setattr(orch._pr_doctor_ctx, "emit", _emit)

    job_id = await orch._pr_doctor_trigger_async("o/r", "feat/x", _pr(), "failing_ci", None)
    await asyncio.gather(*list(orch._pr_doctor_job_tasks))

    job = orch.pr_doctor_job(job_id)
    assert (job["status"], job["error_phase"], job["level"]) == ("error", "spawning", "error")
    assert "failed" in job["message"].lower()


@pytest.mark.asyncio
async def test_pr_doctor_job_unknown_id_is_none():
    assert _orch().pr_doctor_job("prd-nope") is None


@pytest.mark.asyncio
async def test_untrusted_refusal_settles_the_job_for_its_own_target_without_repeating(
        monkeypatch, isolated_db):
    """#3429: the job carries its own target, and the refusal reason is stated once."""
    orch = _orch()

    async def _untrusted(repo, numbers, *, target, head):
        return {n: "untrusted_author" for n in numbers}
    monkeypatch.setattr(orch, "_pr_doctor_untrusted_prs", _untrusted)

    job_id = await orch._pr_doctor_trigger_async(
        "cairnea/KinDash", "feat/x", _pr(number=20904), "merge_conflict", None)
    await asyncio.gather(*list(orch._pr_doctor_job_tasks))

    job = orch.pr_doctor_job(job_id)
    assert job["target"] == "cairnea/KinDash#20904"
    assert (job["status"], job["level"]) == ("error", "warn")
    # Stated once, then which trust list refused it (#3449) — here no product owns it.
    assert job["message"] == ("Not fixing: the PR's author is not a trusted author — no "
                              "single product owns cairnea/KinDash, so no author is "
                              "trusted there")
    assert job["link"] is None  # no owning product, so no list to link to


@pytest.mark.asyncio
async def test_unmirrored_refusal_says_so_and_is_retryable(monkeypatch, isolated_db):
    """#3429: a PR the mirror has not synced yet is not blamed on its author."""
    orch = _orch()

    async def _unmirrored(repo, numbers, *, target, head):
        return {n: "not_mirrored" for n in numbers}
    monkeypatch.setattr(orch, "_pr_doctor_untrusted_prs", _unmirrored)

    job_id = await orch._pr_doctor_trigger_async(
        "cairnea/KinDash", "feat/x", _pr(number=20904), "merge_conflict", None)
    await asyncio.gather(*list(orch._pr_doctor_job_tasks))

    job = orch.pr_doctor_job(job_id)
    assert (job["status"], job["level"]) == ("deferred", "warn")
    assert "isn't mirrored" in job["message"] and "trusted" not in job["message"]
