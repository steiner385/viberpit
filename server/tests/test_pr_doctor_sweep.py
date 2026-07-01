# server/tests/test_pr_doctor_sweep.py
"""PR Doctor per-poll-cycle SWEEP (`_pr_doctor_sweep`).

The poller only fires ``_on_pr_change`` on a CI-``checks`` *transition* to
failing, so a PR that goes BEHIND (out_of_date) or DIRTY (merge_conflict) with
green checks is never classified live. ``_pr_doctor_sweep`` — the ``on_cycle``
hook fired AFTER ``refresh_once`` — walks the fresh cache and runs
``_pr_doctor_step`` on every non-terminal PR, so ALL failure modes are evaluated
every cycle without any checks transition.

Mirrors the ``_build_orch``/``_StubPoller``/``asyncio.run(run())`` + ``tmp_path``
file-DB pattern from ``test_pr_doctor_step.py`` / ``test_pr_doctor_poll_reach.py``.
"""
from __future__ import annotations

import asyncio
import types

from server.github.pr_status import PullRequestInfo
from server.sessions.types import SessionRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pr(number=7, state="open", checks="passing", *, head_sha="sha1",
        merge_state_status="", mergeable="UNKNOWN", is_draft=False):
    return PullRequestInfo(
        number=number,
        url=f"https://x/pull/{number}",
        state=state,
        checks=checks,
        title="t",
        head_sha=head_sha,
        is_draft=is_draft,
        mergeable=mergeable,
        merge_state_status=merge_state_status,
    )


def _make_record(session_id="sess1", name="test-session", repo="o/r",
                 branch="feat/x", authored_branches=None):
    return SessionRecord(
        id=session_id,
        name=name,
        account="acct-a",
        claude_session_id="",
        cols=80,
        rows=24,
        cwd="/tmp",
        repo=repo,
        branch=branch,
        authored_branches=authored_branches or [branch],
    )


class _StubPoller:
    """Multi-entry PR poller stub keyed by (repo, branch)."""

    def __init__(self, cache: dict[tuple[str, str], PullRequestInfo]):
        self._cache = cache

    def cached(self, repo: str, branch: str):
        return self._cache.get((repo, branch))


async def _init_pr_doctor_db(tmp_path) -> None:
    from server.db import init_db
    from server.automations.registry import registry
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sweep.db'}")
    await registry.ensure_rows()


def _build_orch(cache, pairs, *, records=None, drivers=None):
    """Minimal orchestrator with a multi-entry cache + fixed pair list.

    ``_pr_pairs`` is stubbed to return ``pairs`` (bypasses the DB/live-branch
    union so the sweep test drives an exact cache), and ``pr_for`` reads the
    stub poller.
    """
    import server.sessions.orchestrator as orch_mod
    from ccswitch import gitrepos
    from server.automations.context import AutomationContext

    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = records if records is not None else {"sess1": _make_record()}
    orch._pushed_pr = set()
    orch._pr_doctor_ctx = AutomationContext("pr-doctor")
    orch._pr_poller = _StubPoller(cache)  # type: ignore[assignment]
    orch._drivers = drivers if drivers is not None else {"sess1": object()}
    orch._gh_run = gitrepos._run_gh

    async def _pairs():
        return list(pairs)

    orch._pr_pairs = _pairs  # type: ignore[method-assign]
    return orch


async def _set_mode(monkeypatch, mode: str) -> None:
    import server.sessions.orchestrator as orch_mod

    async def _m(self_):
        return mode

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_pr_autofix_mode", _m, raising=True)


async def _set_config(config: dict) -> None:
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        await db.execute(
            sa_update(Automation).where(Automation.id == "pr-doctor").values(config=config)
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_sweep_out_of_date_fires_mechanical_without_checks_transition(monkeypatch, tmp_path):
    """A BEHIND PR with PASSING checks (no CI transition) → the sweep dispatches
    the mechanical update-branch path. Proves a non-CI mode fires autonomously."""

    async def run():
        await _init_pr_doctor_db(tmp_path)
        await _set_mode(monkeypatch, "auto")
        await _set_config({"auto_update_out_of_date": True})

        pr = _pr(merge_state_status="BEHIND", checks="passing")
        cache = {("o/r", "feat/x"): pr}
        orch = _build_orch(cache, [("o/r", "feat/x")])

        gh_calls: list[list[str]] = []

        def fake_gh_run(args):
            gh_calls.append(list(args))
            return types.SimpleNamespace(returncode=0)

        orch._gh_run = fake_gh_run  # type: ignore[assignment]

        sent: list[str] = []

        async def fake_send(session_id, text):
            sent.append(text)
            return "sent"

        orch.send_prompt = fake_send  # type: ignore[method-assign]

        await orch._pr_doctor_sweep()

        assert len(gh_calls) == 1
        assert "update-branch" in " ".join(gh_calls[0])
        assert sent == []  # mechanical never injects
        events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
        assert any(e["kind"] == "updated_branch" for e in events)

    asyncio.run(run())


def test_sweep_merge_conflict_dispatches_session_path(monkeypatch, tmp_path):
    """A DIRTY/CONFLICTING PR (auto_resolve_conflicts on) with a live driver →
    the sweep dispatches the session path (inject the conflict prompt)."""

    async def run():
        from server.sessions.pr_fix import build_conflict_fix_prompt
        await _init_pr_doctor_db(tmp_path)
        await _set_mode(monkeypatch, "auto")
        await _set_config({"auto_resolve_conflicts": True})

        pr = _pr(checks="pending", mergeable="CONFLICTING", merge_state_status="DIRTY")
        cache = {("o/r", "feat/x"): pr}
        orch = _build_orch(cache, [("o/r", "feat/x")])

        sent: list[str] = []

        async def fake_send(session_id, text):
            sent.append(text)
            return "sent"

        orch.send_prompt = fake_send  # type: ignore[method-assign]

        await orch._pr_doctor_sweep()

        assert len(sent) == 1
        assert sent[0] == build_conflict_fix_prompt("o/r", pr)
        assert "conflict" in sent[0].lower()
        st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
        assert st.get("attempts") == 1

    asyncio.run(run())


def test_sweep_skips_terminal_and_healthy(monkeypatch, tmp_path):
    """A merged (terminal) PR and a healthy PR in the cache → the sweep does NOT
    act on either (no gh, no inject, no attempt state)."""

    async def run():
        await _init_pr_doctor_db(tmp_path)
        await _set_mode(monkeypatch, "auto")

        merged = _pr(number=1, state="merged", checks="failing")
        healthy = _pr(number=2, state="open", checks="passing",
                      merge_state_status="CLEAN", mergeable="MERGEABLE")
        cache = {
            ("o/r", "feat/merged"): merged,
            ("o/r", "feat/healthy"): healthy,
        }
        records = {
            "s1": _make_record(session_id="s1", repo="o/r", branch="feat/merged"),
            "s2": _make_record(session_id="s2", repo="o/r", branch="feat/healthy"),
        }
        orch = _build_orch(
            cache,
            [("o/r", "feat/merged"), ("o/r", "feat/healthy")],
            records=records,
            drivers={"s1": object(), "s2": object()},
        )

        gh_calls: list = []
        orch._gh_run = lambda args: gh_calls.append(args)  # type: ignore[assignment]
        sent: list[str] = []

        async def fake_send(session_id, text):
            sent.append(text)
            return "sent"

        orch.send_prompt = fake_send  # type: ignore[method-assign]

        await orch._pr_doctor_sweep()

        assert gh_calls == []
        assert sent == []
        # Terminal PR is skipped before _pr_doctor_step (no terminal event for it).
        merged_events = await orch._pr_doctor_ctx.list_events(target="o/r#1")
        assert merged_events == []
        # Healthy PR was classified but has no recovery mode → no attempt consumed
        # (apply_rearm may seed default counters, but they stay zero — no action).
        st = await orch._pr_doctor_ctx.get_target_state("o/r#2")
        assert st.get("attempts", 0) == 0
        assert st.get("mechanical_attempts", 0) == 0
        assert st.get("last_action_sha") is None

    asyncio.run(run())


def test_sweep_per_pr_error_does_not_abort(monkeypatch, tmp_path):
    """One PR whose _pr_doctor_step raises must not abort the sweep: the second,
    healthy PR is still reached (best-effort per-PR isolation)."""

    async def run():
        await _init_pr_doctor_db(tmp_path)
        await _set_mode(monkeypatch, "auto")

        pr_a = _pr(number=7, merge_state_status="BEHIND", checks="passing")
        pr_b = _pr(number=8, state="open", checks="passing",
                   merge_state_status="CLEAN", mergeable="MERGEABLE")
        cache = {("o/r", "feat/a"): pr_a, ("o/r", "feat/b"): pr_b}
        orch = _build_orch(cache, [("o/r", "feat/a"), ("o/r", "feat/b")],
                           records={}, drivers={})

        reached: list[str] = []
        import server.sessions.orchestrator as orch_mod
        real_step = orch_mod.SessionOrchestrator._pr_doctor_step

        async def flaky_step(self_, repo, branch, info, *, rec=None):
            reached.append(branch)
            if branch == "feat/a":
                raise RuntimeError("boom in first PR")
            return await real_step(self_, repo, branch, info, rec=rec)

        monkeypatch.setattr(orch_mod.SessionOrchestrator, "_pr_doctor_step",
                            flaky_step, raising=True)

        await orch._pr_doctor_sweep()  # must not raise

        assert set(reached) == {"feat/a", "feat/b"}  # both reached despite the raise

    asyncio.run(run())
