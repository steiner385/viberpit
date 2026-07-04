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
from dataclasses import replace

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


class _StubPopulation:
    """Population poller stub: all open PRs per repo (keyed by repo)."""

    def __init__(self, by_repo: dict[str, tuple[PullRequestInfo, ...]] | None = None):
        self._by_repo = by_repo or {}

    def all(self):
        return dict(self._by_repo)

    def cached(self, repo: str):
        return self._by_repo.get(repo, ())


async def _init_pr_doctor_db(tmp_path) -> None:
    from server.db import init_db
    from server.automations.registry import registry
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'sweep.db'}")
    await registry.ensure_rows()
    # The sweep dispatches recovery only when the pr-doctor row is enabled
    # (ensure_rows seeds enabled=False); enable it for the action-path tests.
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        await db.execute(
            sa_update(Automation).where(Automation.id == "pr-doctor").values(enabled=True))


def _build_orch(cache, pairs, *, records=None, drivers=None, population=None):
    """Minimal orchestrator with a multi-entry cache + fixed pair list.

    ``_pr_pairs`` is stubbed to return ``pairs`` (bypasses the DB/live-branch
    union so the sweep test drives an exact cache), ``pr_for`` reads the stub
    poller, and ``_pr_population`` is a stub (empty by default) so
    ``_pr_doctor_scope_pairs`` widens the set to repo-wide open PRs.
    """
    import server.sessions.orchestrator as orch_mod
    from ccswitch import gitrepos
    from server.automations.context import AutomationContext
    from server.automations.action_providers import PrDoctorActionProvider

    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = records if records is not None else {"sess1": _make_record()}
    orch._pushed_pr = set()
    orch._pr_doctor_ctx = AutomationContext("pr-doctor")
    orch._pr_poller = _StubPoller(cache)  # type: ignore[assignment]
    orch._pr_population = _StubPopulation(population)  # type: ignore[assignment]
    orch._drivers = drivers if drivers is not None else {"sess1": object()}
    orch._gh_run = gitrepos._run_gh
    orch._action_providers = {"pr-doctor": PrDoctorActionProvider(orch)}

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

def test_actionable_counts_population_only_failing_pr(tmp_path):
    """A red PR that NO ccswitch session authored (absent from ``_pr_pairs`` and
    the per-branch cache) is still counted: PR Doctor scans every open PR in the
    population cache. An UNSTABLE-failing PR stays excluded (classify unchanged)."""

    async def run():
        await _init_pr_doctor_db(tmp_path)

        failing = _pr(number=383, checks="failing", merge_state_status="BLOCKED")
        failing = replace(failing, head_ref="human/feature")
        unstable = _pr(number=395, checks="failing", merge_state_status="UNSTABLE")
        unstable = replace(unstable, head_ref="human/unstable")

        # No authoring session: empty records, empty per-branch cache, empty _pr_pairs.
        orch = _build_orch(
            cache={}, pairs=[], records={},
            population={"o/r": (failing, unstable)},
        )

        actionable = await orch.automation_actionable("pr-doctor")
        # New shape: [{id, title, url, actions: [{kind, ...}]}]
        ids = {a["id"] for a in actionable}
        assert "o/r#383" in ids          # population-only failing PR is recognized
        assert "o/r#395" not in ids      # UNSTABLE-failing stays non-actionable
        kind = next(a["actions"][0]["kind"] for a in actionable if a["id"] == "o/r#383")
        assert kind == "failing_ci"

    asyncio.run(run())


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


def test_sweep_stashes_failing_count_for_status(monkeypatch, tmp_path):
    """After a sweep the pr-doctor ctx carries a live ``pr_doctor_counts`` so
    ``PrDoctor.status(ctx)`` renders 'watching N failing PR(s)' instead of always
    'idle'. Counts are stashed regardless of mode (a display, not an action)."""

    async def run():
        await _init_pr_doctor_db(tmp_path)
        await _set_mode(monkeypatch, "off")  # off: no recovery, but counts still stash
        failing = _pr(number=7, checks="failing")
        healthy = _pr(number=8, state="open", checks="passing",
                      merge_state_status="CLEAN", mergeable="MERGEABLE")
        cache = {("o/r", "feat/a"): failing, ("o/r", "feat/b"): healthy}
        orch = _build_orch(cache, [("o/r", "feat/a"), ("o/r", "feat/b")],
                           records={}, drivers={})

        await orch._pr_doctor_sweep()

        counts = getattr(orch._pr_doctor_ctx, "pr_doctor_counts", None)
        assert counts is not None
        assert counts["failing"] == 1  # only the failing-CI PR is actionable

        from server.automations.pr_doctor import PrDoctor
        status = PrDoctor().status(orch._pr_doctor_ctx).to_dict()
        assert status["level"] == "active"
        assert "watching 1" in status["text"]

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
