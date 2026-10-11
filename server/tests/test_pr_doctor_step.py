# server/tests/test_pr_doctor_step.py
"""Plan B Task 1: `_pr_doctor_step` dispatch via the pure PR Doctor engine.

Drives `_pr_doctor_step(repo, branch, info, rec=...)` directly with injected
fakes (a `_gh_run` recorder; a fake ctx spawn is a later task). Asserts the right
action branch fires per the engine oracle, that behaviour-preservation holds for
the failing-CI live-driver path, and that repo-scope gating + terminal handling
work. No network, no real session spawn.
"""
from __future__ import annotations

import asyncio

import pytest
from server.github.pr_status import PullRequestInfo
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord

# PR Doctor refuses untrusted/unmirrored PRs; these tests cover its other behaviour.
pytestmark = pytest.mark.usefixtures("pr_authors_trusted")


@pytest.fixture(autouse=True)
def github_identity(monkeypatch):
    """Stable external identities; ownership and reconciliation use real SQL."""
    from server.automations.ownership import CanonicalTarget

    async def resolve(refs, **kwargs):
        return tuple(CanonicalTarget("PR_" + ref.replace("/", "_").replace("#", "_"), ref) for ref in refs)

    monkeypatch.setattr("server.automations.target_identity.resolve_targets", resolve)
    from unittest.mock import AsyncMock
    monkeypatch.setattr("server.automations.model_pick.assess_model", AsyncMock(return_value=None))


def _mechanical_boundary(monkeypatch, orch):
    from server.automations import mechanical_update

    async def snapshot(repo, number, **kwargs):
        return dict(repo=repo, pr_number=number, expected_head_sha="a" * 40,
                    requested_base_sha="b" * 40, mechanical_kind="update_branch")

    async def request(repo, args):
        result = orch._gh_run(args)
        if not hasattr(result, "stdout"):
            result.stdout = ""
        if not hasattr(result, "stderr"):
            result.stderr = ""
        return result

    monkeypatch.setattr(mechanical_update, "snapshot", snapshot)
    monkeypatch.setattr(mechanical_update, "request", request)


# ---------------------------------------------------------------------------
# Helpers (mirror test_pr_autofix.py harness)
# ---------------------------------------------------------------------------

def _pr(number=7, state="open", checks="failing", *, head_sha="a" * 40,
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
        engine=AgentEngine.CLAUDE_CODE,
        engine_session_id="",
        cols=80,
        rows=24,
        cwd="/tmp",
        repo=repo,
        branch=branch,
        authored_branches=authored_branches or [branch],
    )


class _StubPoller:
    def __init__(self, pr: PullRequestInfo, repo="o/r", branch="feat/x"):
        self._pr, self._repo, self._branch = pr, repo, branch

    def cached(self, repo: str, branch: str):
        if repo == self._repo and branch == self._branch:
            return self._pr
        return None


class _StubPopulation:
    """Empty population poller — these tests drive an explicit per-branch cache."""

    def all(self):
        return {}

    def cached(self, repo: str):
        return ()


async def _init_pr_doctor_db(tmp_path) -> None:
    from server.db import init_db
    from server.automations.registry import registry
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'doctor.db'}")
    await registry.ensure_rows()
    # PR Doctor acts only when its row is enabled; ensure_rows seeds enabled=False,
    # so turn it on for the action tests below.
    await _set_enabled(True)


async def _set_enabled(enabled: bool) -> None:
    """Flip the pr-doctor row's ``enabled`` flag (the autonomous-action gate)."""
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        await db.execute(
            sa_update(Automation).where(Automation.id == "pr-doctor").values(enabled=enabled))


def _build_orch(pr, *, has_driver=True):
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext
    from server.automations.action_providers import PrDoctorActionProvider
    from server.automations.pr_doctor_jobs import JobStore

    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = {"sess1": _make_record()}
    orch._pushed_pr = set()
    orch._pr_doctor_ctx = AutomationContext("pr-doctor")
    orch._pr_poller = _StubPoller(pr)  # type: ignore[assignment]
    orch._pr_population = _StubPopulation()  # type: ignore[assignment]
    orch._drivers = {"sess1": object()} if has_driver else {}  # type: ignore[dict-item]
    orch._gh_run = orch_mod.gitrepos._run_gh  # replaced by a recorder per-test
    orch._action_providers = {"pr-doctor": PrDoctorActionProvider(orch)}
    orch._pr_doctor_jobs = JobStore()
    orch._pr_doctor_job_tasks: set[asyncio.Task] = set()
    from server.github.api_budget import PriorityGate
    orch._api_gate = PriorityGate()
    return orch


async def _set_mode(monkeypatch, mode: str) -> None:
    import server.sessions.orchestrator as orch_mod
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        row = await db.get(Automation, "pr-doctor")
        assert row is not None
        row.mode = mode

    async def _m(self_):
        return mode

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_pr_autofix_mode", _m, raising=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_out_of_date_auto_reaches_mechanical_branch(monkeypatch, tmp_path):
    """out_of_date + auto (auto_update_out_of_date default True) → the Mechanical
    branch is dispatched: _gh_run is called with the update-branch argv and NO
    session inject fires."""
    import types
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    pr = _pr(merge_state_status="BEHIND", checks="passing")
    orch = _build_orch(pr, has_driver=True)

    gh_calls: list[list[str]] = []

    def fake_gh_run(args):
        gh_calls.append(list(args))
        return types.SimpleNamespace(returncode=0)

    _mechanical_boundary(monkeypatch, orch)
    orch._gh_run = fake_gh_run  # type: ignore[assignment]
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]

    took = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert took is True   # mechanical success → took_action True
    assert len(gh_calls) == 1                  # gh was called
    assert "update-branch" in " ".join(gh_calls[0])
    assert sent == []      # mechanical never injects a session

    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert any(e["kind"] == "updated_branch" for e in events)


@pytest.mark.asyncio
async def test_failing_ci_auto_live_driver_waits(monkeypatch, tmp_path):
    """A live predecessor defers CI recovery without consuming an attempt."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    pr = _pr(checks="failing", head_sha="sha1")
    orch = _build_orch(pr, has_driver=True)
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]

    took = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert took is False
    assert sent == []

    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert st.get("attempts", 0) == 0

    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert any(e["kind"] == "ownership_wait" for e in events)


@pytest.mark.asyncio
async def test_disabled_row_takes_no_action_even_in_auto(monkeypatch, tmp_path):
    """The enabled gate: a DISABLED pr-doctor row takes NO recovery action even
    with mode=auto and a fixable failing_ci PR + live driver. No inject, no
    attempt recorded, no ``attempted`` event — parity with 'off' mode. This is
    the single chokepoint that also covers the sweep and _on_pr_change."""
    await _init_pr_doctor_db(tmp_path)   # enables the row...
    await _set_enabled(False)            # ...now turn it back off (the gate under test)
    await _set_mode(monkeypatch, "auto")

    pr = _pr(checks="failing", head_sha="sha1")
    orch = _build_orch(pr, has_driver=True)
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]

    took = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert took is False                 # gated out before any dispatch
    assert sent == []                    # no session inject
    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert st.get("attempts", 0) == 0    # no attempt consumed
    assert st.get("last_action_sha") is None
    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert not any(e["kind"] == "attempted" for e in events)


def _patch_ensure_clone(monkeypatch, *, path="/fake/clone/o__r", raises=None):
    """Patch the orchestrator's ``gitrepos.ensure_clone`` so no real git/gh runs.

    Returns a call recorder (list of (repo_root, name_with_owner, url) tuples).
    If ``raises`` is given, ensure_clone raises it instead of returning ``path``.
    """
    import server.sessions.orchestrator as orch_mod

    calls: list[tuple] = []

    def fake_ensure_clone(repo_root, name_with_owner, url):
        calls.append((repo_root, name_with_owner, url))
        if raises is not None:
            raise raises
        return path

    monkeypatch.setattr(orch_mod.gitrepos, "ensure_clone", fake_ensure_clone, raising=True)
    return calls


def _install_spawn_recorder(orch, spawns, *, session_id="spawned-1"):
    """Replace orch._pr_doctor_ctx.spawn_session with an async recorder that
    returns a fake record with an id, and honours de-dup (returns the same fake
    for a repeated dedupe_key without re-appending)."""
    import types as _types
    seen: dict[str, object] = {}

    async def fake_spawn(*, origin_ref, dedupe_key=None, **kwargs):
        key = kwargs["ownership_token"]
        if key in seen:
            return seen[key]
        from server.automations.ownership import owned
        async with owned(kwargs["ownership_token"]) as tx:
            sid = tx.reserve_session()
            tx.bind_session(sid)
        rec = _types.SimpleNamespace(id=sid)
        seen[key] = rec
        spawns.append({"origin_ref": origin_ref, "dedupe_key": dedupe_key, **kwargs})
        return rec

    orch._pr_doctor_ctx.spawn_session = fake_spawn  # type: ignore[method-assign]
    return fake_spawn


@pytest.mark.asyncio
async def test_failing_ci_auto_no_driver_spawns_deduped(monkeypatch, tmp_path):
    """failing_ci + auto with NO live driver → spawn a fresh session via
    ctx.spawn_session with origin_ref/dedupe_key == target and the CI-fix prompt.
    A second identical poll de-dups (spawn recorder called once total). No
    inject; attempt consumed on the real spawn; `spawned`/`attempted` accounting."""
    from server.sessions.pr_fix import build_pr_fix_prompt
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    pr = _pr(checks="failing", head_sha="sha1")
    orch = _build_orch(pr, has_driver=False)
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]
    spawns: list[dict] = []
    _install_spawn_recorder(orch, spawns)
    clone_calls = _patch_ensure_clone(monkeypatch)

    took = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert took is True
    assert sent == []  # spawn, not inject into a (nonexistent) live driver
    assert len(spawns) == 1
    assert spawns[0]["origin_ref"] == "o/r#7"
    assert spawns[0]["dedupe_key"] == "o/r#7"
    # The clone cwd was resolved and forwarded to the spawn (NOT None), plus the
    # PR branch — so create_session builds a real worktree, not an empty scratch.
    assert clone_calls == []  # create_session owns clone after reservation
    assert spawns[0]["repo"] == "o/r"
    assert spawns[0]["branch"] == "feat/x"
    # The seeded first prompt is the CI-fix prompt.
    assert spawns[0]["initial_prompt"] == build_pr_fix_prompt("o/r", pr)
    assert "gh pr checkout 7" in spawns[0]["initial_prompt"]

    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert st.get("attempts") == 1
    assert st.get("last_action_sha") == "sha1"
    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert any(e["kind"] == "attempted" for e in events)

    # Second identical poll (same head) → per-head guard holds, no second spawn.
    took2 = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert took2 is False
    assert len(spawns) == 1  # de-dup / guard: spawned once total


@pytest.mark.asyncio
async def test_merge_conflict_auto_no_driver_spawns_conflict_prompt(monkeypatch, tmp_path):
    """merge_conflict + auto + auto_resolve_conflicts=True + no live driver →
    spawn with the CONFLICT prompt (build_conflict_fix_prompt), not the CI prompt."""
    from server.sessions.pr_fix import build_conflict_fix_prompt, build_pr_fix_prompt
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        await db.execute(
            sa_update(Automation)
            .where(Automation.id == "pr-doctor")
            .values(config={"auto_resolve_conflicts": True})
        )

    pr = _pr(checks="pending", mergeable="CONFLICTING", merge_state_status="DIRTY",
             head_sha="sha1")
    orch = _build_orch(pr, has_driver=False)
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]
    spawns: list[dict] = []
    _install_spawn_recorder(orch, spawns)
    _patch_ensure_clone(monkeypatch)

    took = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert took is True
    assert sent == []
    assert len(spawns) == 1
    assert spawns[0]["origin_ref"] == "o/r#7"
    assert spawns[0]["repo"] == "o/r"
    prompt = spawns[0]["initial_prompt"]
    assert prompt == build_conflict_fix_prompt("o/r", pr)
    assert prompt != build_pr_fix_prompt("o/r", pr)
    assert "conflict" in prompt.lower()

    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert st.get("attempts") == 1


@pytest.mark.asyncio
async def test_failing_ci_no_driver_clone_failure_does_not_consume_attempt(monkeypatch, tmp_path):
    """A failed session launch consumes no attempt; the caller does not clone."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    pr = _pr(checks="failing", head_sha="sha1")
    orch = _build_orch(pr, has_driver=False)
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]
    spawns: list[dict] = []
    async def failed_spawn(**kwargs):
        raise RuntimeError("boom")
    orch._pr_doctor_ctx.spawn_session = failed_spawn
    clone_calls = _patch_ensure_clone(monkeypatch, raises=RuntimeError("boom"))

    took = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert took is False           # fell through to notify
    assert len(clone_calls) == 0   # the clone was attempted
    assert spawns == []            # spawn NOT reached
    assert sent == []              # no inject

    # Attempt NOT consumed — the doomed spawn didn't burn the give-up budget.
    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert st.get("attempts", 0) == 0
    assert st.get("total_attempts", 0) == 0

    # An error/spawn_failed event is emitted.
    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert any(e["kind"] == "spawn_failed" for e in events)
    assert any(e.get("level") == "error" for e in events)


@pytest.mark.asyncio
async def test_merge_conflict_auto_live_driver_waits(monkeypatch, tmp_path):
    """A live predecessor also defers conflict recovery without competing work."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        await db.execute(
            sa_update(Automation)
            .where(Automation.id == "pr-doctor")
            .values(config={"auto_resolve_conflicts": True})
        )

    pr = _pr(checks="pending", mergeable="CONFLICTING", merge_state_status="DIRTY",
             head_sha="sha1")
    orch = _build_orch(pr, has_driver=True)
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]
    spawns: list[dict] = []
    _install_spawn_recorder(orch, spawns)

    took = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert took is False
    assert spawns == []  # live predecessor blocks both effects
    assert sent == []

    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert st.get("attempts", 0) == 0


@pytest.mark.asyncio
async def test_failing_ci_auto_no_driver_does_not_inject(monkeypatch, tmp_path):
    """failing_ci + auto but NO live driver and NO spawn wiring → no inject.

    (Regression guard for the inject path only: with the spawn recorder absent
    the spawn branch still must not call send_prompt.)"""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    pr = _pr(checks="failing")
    orch = _build_orch(pr, has_driver=False)
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]
    spawns: list[dict] = []
    _install_spawn_recorder(orch, spawns)
    _patch_ensure_clone(monkeypatch)

    took = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    # With a spawn recorder wired, the no-driver path now spawns.
    assert took is True
    assert sent == []


@pytest.mark.asyncio
async def test_repeated_live_predecessor_waits_do_not_consume_retry_budget(monkeypatch, tmp_path):
    """Repeated failing heads preserve the live predecessor and the retry budget."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    orch = _build_orch(_pr(head_sha="sha0"), has_driver=True)
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]

    # Six polls, each a distinct still-failing head (agent pushes a red fix each time).
    for i in range(6):
        pr = _pr(checks="failing", head_sha=f"sha{i + 1}")
        orch._pr_poller = _StubPoller(pr)  # type: ignore[assignment]
        await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])

    # Waiting is not an attempted repair.
    assert sent == []

    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert not st.get("gave_up")
    assert st.get("total_attempts", 0) == 0

    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert any(e["kind"] == "ownership_wait" for e in events)


@pytest.mark.asyncio
async def test_failing_ci_one_attempt_per_head(monkeypatch, tmp_path):
    """Two polls of the same head spawn one fenced repair session."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    pr = _pr(checks="failing", head_sha="sha1")
    orch = _build_orch(pr, has_driver=False)
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]
    spawns = []
    _install_spawn_recorder(orch, spawns)

    await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert sent == []
    assert len(spawns) == 1


@pytest.mark.asyncio
async def test_external_push_does_not_override_live_predecessor(monkeypatch, tmp_path):
    """A changed head does not authorize injection into a live predecessor."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    pr1 = _pr(checks="failing", head_sha="sha1")
    orch = _build_orch(pr1, has_driver=True)
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]

    await orch._pr_doctor_step("o/r", "feat/x", pr1, rec=orch._records["sess1"])
    pr2 = _pr(checks="failing", head_sha="sha2")  # external push
    await orch._pr_doctor_step("o/r", "feat/x", pr2, rec=orch._records["sess1"])
    assert sent == []
    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert any(e["kind"] == "ownership_wait" for e in events)


@pytest.mark.asyncio
async def test_merge_conflict_auto_default_proposes(monkeypatch, tmp_path):
    """merge_conflict + auto with the default cfg (auto_resolve_conflicts False)
    → Propose (event, no gh, no inject)."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    pr = _pr(checks="pending", mergeable="CONFLICTING", merge_state_status="DIRTY")
    orch = _build_orch(pr, has_driver=True)
    gh_calls: list[list[str]] = []
    orch._gh_run = lambda args: gh_calls.append(args)  # type: ignore[assignment]
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]

    took = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert took is False
    assert gh_calls == []
    assert sent == []
    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert any(e["kind"] == "proposed" for e in events)


@pytest.mark.asyncio
async def test_mode_off_never_acts(monkeypatch, tmp_path):
    """mode off → Notify only: no inject, no gh, no attempt."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "off")

    pr = _pr(checks="failing")
    orch = _build_orch(pr, has_driver=True)
    gh_calls: list[list[str]] = []
    orch._gh_run = lambda args: gh_calls.append(args)  # type: ignore[assignment]
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]

    took = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert took is False
    assert gh_calls == []
    assert sent == []


@pytest.mark.asyncio
async def test_terminal_pr_clears_state(monkeypatch, tmp_path):
    """A merged/closed PR → terminal event, target state cleared, no action."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    # seed some prior attempt state
    orch = _build_orch(_pr(), has_driver=True)
    await orch._pr_doctor_ctx.set_target_state("o/r#7", {"attempts": 2})

    merged = _pr(state="merged", checks="failing")
    took = await orch._pr_doctor_step("o/r", "feat/x", merged, rec=orch._records["sess1"])
    assert took is False
    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert st == {}
    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert not any(e["kind"] == "attempted" for e in events)


@pytest.mark.asyncio
async def test_repo_scope_gate_excludes_repo(monkeypatch, tmp_path):
    """A non-empty enabled_repos that excludes this repo → treated as off."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    # Set the pr-doctor row config to a repo allowlist that excludes o/r.
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        await db.execute(sa_update(Automation).where(Automation.id == "pr-doctor")
                         .values(config={"enabled_repos": ["other/repo"]}))

    pr = _pr(checks="failing")
    orch = _build_orch(pr, has_driver=True)
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]

    took = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert took is False
    assert sent == []


# ---------------------------------------------------------------------------
# Task 2: Mechanical update-branch executor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_out_of_date_mechanical_success(monkeypatch, tmp_path):
    """out_of_date + auto + auto_update_out_of_date=True + gh returns 0:
    - _gh_run called with the update-branch API argv
    - NO session send/inject
    - mechanical_attempts and total_mechanical both incremented to 1
    - updated_branch event emitted
    - took_action True
    """
    import types
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    # Enable auto_update_out_of_date via the automation config row.
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        await db.execute(
            sa_update(Automation)
            .where(Automation.id == "pr-doctor")
            .values(config={"auto_update_out_of_date": True})
        )

    pr = _pr(merge_state_status="BEHIND", checks="passing")
    orch = _build_orch(pr, has_driver=True)

    gh_calls: list[list[str]] = []

    def fake_gh_run(args):
        gh_calls.append(list(args))
        return types.SimpleNamespace(returncode=0)

    _mechanical_boundary(monkeypatch, orch)
    orch._gh_run = fake_gh_run  # type: ignore[assignment]

    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]

    took = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert took is True
    # gh was called with the update-branch API path
    assert len(gh_calls) == 1
    argv = gh_calls[0]
    assert "update-branch" in " ".join(argv)
    assert any("o/r" in a for a in argv)
    assert any("7" in a for a in argv)  # PR number
    # No session inject
    assert sent == []
    # Attempt counters incremented
    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert st.get("mechanical_attempts") == 1
    assert st.get("total_mechanical") == 1
    # updated_branch event emitted
    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert any(e["kind"] == "updated_branch" for e in events)


@pytest.mark.asyncio
async def test_out_of_date_mechanical_gh_failure(monkeypatch, tmp_path):
    """out_of_date + auto + auto_update_out_of_date=True + gh returns non-zero:
    - warn / mechanical_failed event emitted
    - attempt NOT consumed (mechanical_attempts stays 0)
    - took_action False (falls through to notify)
    """
    import types
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        await db.execute(
            sa_update(Automation)
            .where(Automation.id == "pr-doctor")
            .values(config={"auto_update_out_of_date": True})
        )

    pr = _pr(merge_state_status="BEHIND", checks="passing")
    orch = _build_orch(pr, has_driver=True)

    def fake_gh_run_fail(args):
        return types.SimpleNamespace(returncode=1)

    _mechanical_boundary(monkeypatch, orch)
    orch._gh_run = fake_gh_run_fail  # type: ignore[assignment]

    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]

    took = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert took is False
    # No session inject
    assert sent == []
    # Attempt NOT consumed
    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert st.get("mechanical_attempts", 0) == 0
    # warn event with mechanical_failed emitted
    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert any(e["kind"] == "mechanical_failed" for e in events)
    warn_events = [e for e in events if e.get("level") == "warn"]
    assert len(warn_events) >= 1


# ---------------------------------------------------------------------------
# Task 5/6: propose-surface executors via generic dispatch
# (previously pr_doctor_fix / pr_doctor_actionable — now automation_trigger /
#  automation_actionable; logic lives in PrDoctorActionProvider)
# ---------------------------------------------------------------------------

async def _pr_pairs_stub(orch, pairs):
    """Wire orch._pr_pairs to return a fixed list (used by the propose executors)."""
    async def _pairs():
        return list(pairs)
    orch._pr_pairs = _pairs  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_pr_doctor_fix_out_of_date_runs_mechanical(monkeypatch, tmp_path):
    """automation_trigger(out_of_date) on an actionable PR → the SAME mechanical
    update-branch executor runs (gh called), result status is 'updated'."""
    import types
    await _init_pr_doctor_db(tmp_path)

    pr = _pr(merge_state_status="BEHIND", checks="passing")
    orch = _build_orch(pr, has_driver=True)
    await _pr_pairs_stub(orch, [("o/r", "feat/x")])

    gh_calls: list[list[str]] = []

    def fake_gh_run(args):
        gh_calls.append(list(args))
        return types.SimpleNamespace(returncode=0)

    _mechanical_boundary(monkeypatch, orch)
    orch._gh_run = fake_gh_run  # type: ignore[assignment]

    result = await orch.automation_trigger("pr-doctor", "o/r#7", "out_of_date")
    assert result["status"] == "updated"
    assert len(gh_calls) == 1
    assert "update-branch" in " ".join(gh_calls[0])


@pytest.mark.asyncio
async def test_pr_doctor_fix_live_driver_settles_waiting(monkeypatch, tmp_path):
    """Manual repair returns a job, then reports the live ownership wait."""
    await _init_pr_doctor_db(tmp_path)

    pr = _pr(checks="failing", head_sha="sha1")
    orch = _build_orch(pr, has_driver=True)
    await _pr_pairs_stub(orch, [("o/r", "feat/x")])
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]

    # automation_trigger now returns immediately with a job_id ("queued").
    result = await orch.automation_trigger("pr-doctor", "o/r#7", "failing_ci")
    assert result["status"] == "queued"
    assert "job_id" in result

    # Drain the background task, then check that send_prompt was called.
    await asyncio.gather(*list(orch._pr_doctor_job_tasks))
    assert sent == []
    job = orch.pr_doctor_job(result["job_id"])
    assert job is not None
    assert job["status"] == "deferred"
    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert any(e["kind"] == "ownership_wait" for e in events)


@pytest.mark.asyncio
async def test_pr_doctor_fix_terminal_pr_is_noop(monkeypatch, tmp_path):
    """A merged/closed PR is never acted on → 'not_actionable', no gh, no inject."""
    await _init_pr_doctor_db(tmp_path)

    pr = _pr(state="merged", checks="failing")
    orch = _build_orch(pr, has_driver=True)
    await _pr_pairs_stub(orch, [("o/r", "feat/x")])

    gh_calls: list = []
    orch._gh_run = lambda args: gh_calls.append(args)  # type: ignore[assignment]
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]

    result = await orch.automation_trigger("pr-doctor", "o/r#7", "out_of_date")
    assert result["status"] == "not_actionable"
    assert gh_calls == []
    assert sent == []


@pytest.mark.asyncio
async def test_pr_doctor_fix_unknown_pr(monkeypatch, tmp_path):
    """No cached PR for the number → 'no_pr' status via automation_trigger."""
    await _init_pr_doctor_db(tmp_path)
    pr = _pr(number=7)
    orch = _build_orch(pr, has_driver=True)
    await _pr_pairs_stub(orch, [("o/r", "feat/x")])
    result = await orch.automation_trigger("pr-doctor", "o/r#999", "failing_ci")
    assert result["status"] == "no_pr"


@pytest.mark.asyncio
async def test_pr_doctor_actionable_lists_real_recovery_modes(monkeypatch, tmp_path):
    """automation_actionable classifies each cached PR and returns only real
    recovery modes (failing_ci/out_of_date/merge_conflict), not healthy/blocked."""
    await _init_pr_doctor_db(tmp_path)

    failing = _pr(number=7, checks="failing", head_sha="s7")
    behind = _pr(number=9, checks="passing", merge_state_status="BEHIND", head_sha="s9")
    healthy = _pr(number=11, checks="passing", head_sha="s11")

    orch = _build_orch(failing, has_driver=True)

    cache = {
        ("o/r", "feat/a"): failing,
        ("o/r", "feat/b"): behind,
        ("o/r", "feat/c"): healthy,
    }
    orch.pr_for = lambda repo, br: cache.get((repo, br))  # type: ignore[method-assign]
    await _pr_pairs_stub(orch, list(cache.keys()))

    out = await orch.automation_actionable("pr-doctor")
    # out is list of ActionableItem dicts: {id, title, subtitle, url, actions}
    by_id = {d["id"]: d for d in out}
    assert "o/r#7" in by_id   # failing PR included
    assert "o/r#9" in by_id   # behind PR included
    assert "o/r#11" not in by_id  # healthy PR excluded
    assert by_id["o/r#7"]["actions"][0]["kind"] == "failing_ci"
    assert by_id["o/r#9"]["actions"][0]["kind"] == "out_of_date"
    assert by_id["o/r#7"]["url"] and by_id["o/r#7"]["title"]


# ---------------------------------------------------------------------------
# Regression tests for: queued PRs, no-new-commits 422, transient spawn error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_behind_pr_in_merge_queue_is_no_op(monkeypatch, tmp_path):
    """A BEHIND PR that is already in the merge queue must NOT trigger update-branch.

    GitHub rejects update-branch with 422 for queued PRs. classify() now returns
    None (no action) so the step exits without calling _gh_run.
    """
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        await db.execute(
            sa_update(Automation)
            .where(Automation.id == "pr-doctor")
            .values(config={"auto_update_out_of_date": True})
        )

    # BEHIND but already in_merge_queue → classify() should return None
    pr = PullRequestInfo(
        number=7, url="https://x/pull/7", state="open", checks="passing",
        title="t", head_sha="a" * 40, is_draft=False, mergeable="MERGEABLE",
        merge_state_status="BEHIND", in_merge_queue=True,
    )
    orch = _build_orch(pr, has_driver=True)
    orch._pr_poller = _StubPoller(pr)  # type: ignore[assignment]

    gh_calls: list = []
    orch._gh_run = lambda args: gh_calls.append(args)  # type: ignore[assignment]

    took = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert took is False        # no action taken
    assert gh_calls == []       # gh never called


@pytest.mark.asyncio
async def test_update_branch_no_new_commits_consumes_attempt(monkeypatch, tmp_path):
    """gh update-branch 422 'no new commits' is treated as a done state.

    The branch was already current; returning True (success) lets record_attempt
    fire so already_acted prevents infinite retries on the same head SHA.
    """
    import types
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        await db.execute(
            sa_update(Automation)
            .where(Automation.id == "pr-doctor")
            .values(config={"auto_update_out_of_date": True})
        )

    pr = _pr(merge_state_status="BEHIND", checks="passing")
    orch = _build_orch(pr, has_driver=True)

    def fake_gh_no_new(args):
        return types.SimpleNamespace(
            returncode=1,
            stdout='{"message":"There are no new commits on the base branch."}',
            stderr="gh: no new commits (HTTP 422)",
        )

    _mechanical_boundary(monkeypatch, orch)
    orch._gh_run = fake_gh_no_new  # type: ignore[assignment]

    took = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert took is True   # treated as done, not a failure

    # Attempt was consumed — already_acted will block next poll on same SHA
    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert st.get("mechanical_attempts", 0) == 1

    # Event emitted as updated_branch (not mechanical_failed)
    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert any(e["kind"] == "updated_branch" for e in events)
    assert not any(e["kind"] == "mechanical_failed" for e in events)


# ---------------------------------------------------------------------------
# #2912 / #2920: re-check LIVE PR state before a session fix; dedupe durably
# ---------------------------------------------------------------------------

def _live(orch, live: PullRequestInfo | None) -> list[tuple[str, int]]:
    """Answer ``_pr_doctor_live_pr`` with *live*; returns the call log."""
    calls: list[tuple[str, int]] = []

    async def fake_live(repo, number):
        calls.append((repo, number))
        return live

    orch._pr_doctor_live_pr = fake_live  # type: ignore[method-assign]
    return calls


async def _stop_repair(orch, target="o/r#7") -> None:
    """The target's repair session ran and stopped its managed work (a durable
    stopped-work attestation), so a later claim may take the target over — the
    predecessor is not what blocks a re-dispatch."""
    from server.automations.ownership import (
        attest_session_stopped, begin_session_execution, execution_snapshot)
    sid = (await orch._pr_doctor_ctx.get_target_state(target))["session_id"]
    await begin_session_execution(sid)
    snapshot = await execution_snapshot(sid)
    assert snapshot is not None and snapshot.generation is not None
    assert await attest_session_stopped(snapshot.token, snapshot.generation, session_id=sid)


async def _enable_conflict_fixes() -> None:
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        await db.execute(sa_update(Automation).where(Automation.id == "pr-doctor")
                         .values(config={"auto_resolve_conflicts": True}))


@pytest.mark.asyncio
async def test_2920_failing_ci_not_dispatched_for_a_pr_that_already_merged(monkeypatch, tmp_path):
    """#2920: the cached snapshot still read open + failing, but the PR had merged
    green. The live re-check skips the dispatch with an info event and consumes
    no attempt."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")
    pr = _pr(checks="failing", head_sha="sha1")
    orch = _build_orch(pr, has_driver=False)
    spawns: list[dict] = []
    _install_spawn_recorder(orch, spawns)
    calls = _live(orch, _pr(state="merged", checks="passing", head_sha="sha1"))

    took = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert took is False
    assert spawns == []
    assert calls == [("o/r", 7)]
    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert st.get("total_attempts", 0) == 0
    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    skipped = [e for e in events if e["kind"] == "skipped"]
    assert len(skipped) == 1 and skipped[0]["level"] == "info"
    assert skipped[0]["detail"]["reason"] == "merged"

    # Repeated sweeps over the same stale snapshot report it once.
    await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert len([e for e in events if e["kind"] == "skipped"]) == 1
    assert spawns == []


@pytest.mark.asyncio
async def test_2920_live_read_is_one_graphql_call_and_fails_open(monkeypatch):
    """The real seam: one ``gh api graphql`` for the PR, parsed through the shared
    node fields; any gh failure answers None (fail open)."""
    import json
    import types

    import server.sessions.orchestrator as orch_mod
    real = orch_mod.SessionOrchestrator._pr_doctor_live_pr.__wrapped__  # conftest stubs it
    orch = _build_orch(_pr())
    monkeypatch.setattr(orch_mod.gitrepos, "app_token_for_owner", lambda owner: None)
    node = {"number": 7, "url": "u", "title": "t", "state": "MERGED", "isDraft": False,
            "headRefOid": "sha1", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}
    calls: list[list[str]] = []

    def gh_ok(args, token=None, timeout=None):
        calls.append(list(args))
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(
            {"data": {"repository": {"pullRequest": node}}}))

    orch._gh_run = gh_ok  # type: ignore[assignment]
    live = await real(orch, "o/r", 7)
    assert live is not None and (live.state, live.head_sha) == ("merged", "sha1")
    assert len(calls) == 1 and calls[0][:2] == ["api", "graphql"]
    assert "pullRequest(number: 7)" in calls[0][-1]

    orch._gh_run = lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="")  # type: ignore[assignment]
    assert await real(orch, "o/r", 7) is None

    def gh_boom(*a, **k):
        raise OSError("gh missing")

    orch._gh_run = gh_boom  # type: ignore[assignment]
    assert await real(orch, "o/r", 7) is None


@pytest.mark.asyncio
async def test_2920_failing_ci_not_dispatched_when_live_checks_went_green(monkeypatch, tmp_path):
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")
    pr = _pr(checks="failing", head_sha="sha1")
    orch = _build_orch(pr, has_driver=False)
    spawns: list[dict] = []
    _install_spawn_recorder(orch, spawns)
    _live(orch, _pr(checks="passing", head_sha="sha1", merge_state_status="CLEAN"))

    assert await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"]) is False
    assert spawns == []


@pytest.mark.asyncio
async def test_2912_merge_conflict_not_dispatched_once_live_pr_is_mergeable(monkeypatch, tmp_path):
    """#2912: the conflict was already resolved (live MERGEABLE at the same head)."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")
    await _enable_conflict_fixes()
    pr = _pr(checks="pending", mergeable="CONFLICTING", merge_state_status="DIRTY",
             head_sha="sha1")
    orch = _build_orch(pr, has_driver=False)
    spawns: list[dict] = []
    _install_spawn_recorder(orch, spawns)
    _live(orch, _pr(checks="pending", mergeable="MERGEABLE", merge_state_status="BLOCKED",
                    head_sha="sha1"))

    assert await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"]) is False
    assert spawns == []
    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert [e["detail"]["reason"] for e in events if e["kind"] == "skipped"] == ["resolved"]


@pytest.mark.asyncio
async def test_2912_merge_conflict_dispatch_survives_a_class_flip_at_the_same_head(
        monkeypatch, tmp_path):
    """#2912: merge_conflict -> failing_ci -> merge_conflict snapshots at ONE head
    re-armed the per-head guard and re-sent the identical conflict prompt."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")
    await _enable_conflict_fixes()
    conflict = _pr(checks="pending", mergeable="CONFLICTING", merge_state_status="DIRTY",
                   head_sha="sha1")
    red = _pr(checks="failing", mergeable="MERGEABLE", merge_state_status="CLEAN",
              head_sha="sha1")
    orch = _build_orch(conflict, has_driver=False)
    spawns: list[dict] = []
    _install_spawn_recorder(orch, spawns)
    _live(orch, None)  # live read unavailable: only the durable dedupe stands

    assert await orch._pr_doctor_step("o/r", "feat/x", conflict, rec=None) is True
    await _stop_repair(orch)
    # A real CI red at the same head is a different problem: one legitimate
    # failing_ci repair, whose class change re-arms the re-armable `attempts`.
    assert await orch._pr_doctor_step("o/r", "feat/x", red, rec=None) is True
    await _stop_repair(orch)
    # The stale DIRTY snapshot again: the conflict was already handled at sha1.
    assert await orch._pr_doctor_step("o/r", "feat/x", conflict, rec=None) is False
    assert [s["initial_prompt"].split(" has ")[1][:5] for s in spawns] == ["a MER", "faili"]


@pytest.mark.asyncio
async def test_2920_failing_ci_not_redispatched_at_same_head_after_recovery_clear(
        monkeypatch, tmp_path):
    """#2920: the first fix was a rerun of a flaky shard — CI went green at the
    SAME head and the recovery clear wiped ``last_action_sha``, so a later stale
    red at that head dispatched again. The (kind, head) dedupe must survive the
    clear; a new head legitimately re-arms it."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")
    pr = _pr(checks="failing", head_sha="sha1")
    orch = _build_orch(pr, has_driver=False)
    spawns: list[dict] = []
    _install_spawn_recorder(orch, spawns)
    _live(orch, None)

    assert await orch._pr_doctor_step("o/r", "feat/x", pr, rec=None) is True
    await _stop_repair(orch)
    # The rerun went green at the same head: the poller's recovery transition
    # clears the target (the real clear, not a hand-written state).
    await orch._on_pr_change("o/r", "feat/x", "failing", "passing")
    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert "ownership_token" not in st and not st.get("attempts")

    assert await orch._pr_doctor_step("o/r", "feat/x", pr, rec=None) is False
    assert len(spawns) == 1

    pr2 = _pr(checks="failing", head_sha="sha2")
    _live(orch, pr2)
    assert await orch._pr_doctor_step("o/r", "feat/x", pr2, rec=None) is True
    assert len(spawns) == 2


@pytest.mark.asyncio
async def test_2912_live_read_not_spent_on_a_pr_blocked_by_a_cheap_refusal(monkeypatch, tmp_path):
    """The live GraphQL read sits AFTER the cheap refusals: a PR held by a live
    predecessor or an untrusted author refuses every sweep, and must not spend a
    GitHub call each time (~48/hr outside the shared API budget)."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")
    pr = _pr(checks="failing", head_sha="sha1")

    # Live predecessor still attached.
    orch = _build_orch(pr, has_driver=True)
    spawns: list[dict] = []
    _install_spawn_recorder(orch, spawns)
    calls = _live(orch, pr)
    for _ in range(3):
        assert await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"]) is False
    assert spawns == [] and calls == []

    # Untrusted author.
    async def untrusted(_db, _repo, numbers):
        return list(numbers)
    monkeypatch.setattr("server.pm.dispatch_trust.untrusted_prs", untrusted)
    orch = _build_orch(pr, has_driver=False)
    _install_spawn_recorder(orch, spawns)
    calls = _live(orch, pr)
    for _ in range(3):
        assert await orch._pr_doctor_step("o/r", "feat/x", pr, rec=None) is False
    assert spawns == [] and calls == []


@pytest.mark.asyncio
async def test_2912_live_read_skip_releases_the_claim_without_an_attempt(monkeypatch, tmp_path):
    """A live-state skip after the claim is a clean not-started: no attempt, no
    ownership left behind, so the next sweep can re-judge."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")
    pr = _pr(checks="failing", head_sha="sha1")
    orch = _build_orch(pr, has_driver=False)
    spawns: list[dict] = []
    _install_spawn_recorder(orch, spawns)
    _live(orch, _pr(checks="passing", head_sha="sha1", merge_state_status="CLEAN"))

    assert await orch._pr_doctor_step("o/r", "feat/x", pr, rec=None) is False
    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert "ownership_token" not in st
    assert not st.get("total_attempts") and not st.get("acted_heads")
    assert spawns == []


@pytest.mark.asyncio
async def test_2912_live_read_yields_to_the_shared_api_gate(monkeypatch):
    """The live read shares the GitHub-App budget with the PR pollers: it waits
    (bounded) for an in-flight high-priority PR-status refresh to finish."""
    import json
    import types

    import server.sessions.orchestrator as orch_mod
    real = orch_mod.SessionOrchestrator._pr_doctor_live_pr.__wrapped__  # conftest stubs it
    orch = _build_orch(_pr())  # carries a real shared PriorityGate
    monkeypatch.setattr(orch_mod.gitrepos, "app_token_for_owner", lambda owner: None)
    calls: list[list[str]] = []

    def gh_ok(args, token=None, timeout=None):
        calls.append(list(args))
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(
            {"data": {"repository": {"pullRequest": None}}}))

    orch._gh_run = gh_ok  # type: ignore[assignment]
    release = asyncio.Event()

    async def refresh():
        async with orch._api_gate.high_priority():
            await release.wait()

    holder = asyncio.create_task(refresh())
    await asyncio.sleep(0)
    reader = asyncio.create_task(real(orch, "o/r", 7))
    for _ in range(5):
        await asyncio.sleep(0)
    assert calls == []           # parked behind the PR-status refresh
    release.set()
    await holder
    assert await reader is None  # missing PR parses to None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_2912_dedupe_skip_is_reported_once_per_pr_kind_and_head(monkeypatch, tmp_path):
    """A (kind, head) dedupe skip is not silent: one info event per (PR, kind,
    head), not one per sweep — and a new head reports afresh."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")
    pr = _pr(checks="failing", head_sha="sha1")
    orch = _build_orch(pr, has_driver=False)
    spawns: list[dict] = []
    _install_spawn_recorder(orch, spawns)
    _live(orch, None)

    assert await orch._pr_doctor_step("o/r", "feat/x", pr, rec=None) is True
    for _ in range(3):
        assert await orch._pr_doctor_step("o/r", "feat/x", pr, rec=None) is False
    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    dup = [e for e in events if e["kind"] == "skipped"
           and e["detail"].get("reason") == "already_dispatched"]
    assert len(dup) == 1 and dup[0]["level"] == "info"
    assert dup[0]["detail"]["failure"] == "failing_ci"
    assert dup[0]["detail"]["head_sha"] == "sha1"
    assert len(spawns) == 1
