# server/tests/test_pr_doctor_step.py
"""Plan B Task 1: `_pr_doctor_step` dispatch via the pure PR Doctor engine.

Drives `_pr_doctor_step(repo, branch, info, rec=...)` directly with injected
fakes (a `_gh_run` recorder; a fake ctx spawn is a later task). Asserts the right
action branch fires per the engine oracle, that behaviour-preservation holds for
the failing-CI live-driver path, and that repo-scope gating + terminal handling
work. No network, no real session spawn.
"""
from __future__ import annotations

import pytest
from server.github.pr_status import PullRequestInfo
from server.sessions.types import SessionRecord


# ---------------------------------------------------------------------------
# Helpers (mirror test_pr_autofix.py harness)
# ---------------------------------------------------------------------------

def _pr(number=7, state="open", checks="failing", *, head_sha="sha1",
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


def _build_orch(pr, *, has_driver=True):
    import server.sessions.orchestrator as orch_mod
    from ccswitch import gitrepos
    from server.automations.context import AutomationContext
    from server.automations.action_providers import PrDoctorActionProvider

    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = {"sess1": _make_record()}
    orch._pushed_pr = set()
    orch._pr_doctor_ctx = AutomationContext("pr-doctor")
    orch._pr_poller = _StubPoller(pr)  # type: ignore[assignment]
    orch._pr_population = _StubPopulation()  # type: ignore[assignment]
    orch._drivers = {"sess1": object()} if has_driver else {}  # type: ignore[dict-item]
    orch._gh_run = gitrepos._run_gh  # replaced by a recorder per-test
    orch._action_providers = {"pr-doctor": PrDoctorActionProvider(orch)}
    return orch


async def _set_mode(monkeypatch, mode: str) -> None:
    import server.sessions.orchestrator as orch_mod

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
async def test_failing_ci_auto_live_driver_injects(monkeypatch, tmp_path):
    """failing_ci + auto + live driver → inject via request_pr_fix, attempt
    recorded, `attempted` emitted, took_action True. (Behaviour preserved.)"""
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
    assert took is True
    assert len(sent) == 1
    assert "gh pr checkout 7" in sent[0]

    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert st.get("attempts") == 1
    assert st.get("last_action_sha") == "sha1"

    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert any(e["kind"] == "attempted" for e in events)


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
        key = dedupe_key or origin_ref
        if key in seen:
            return seen[key]
        rec = _types.SimpleNamespace(id=session_id)
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
    assert len(clone_calls) == 1
    assert clone_calls[0][1] == "o/r"
    assert spawns[0]["cwd"] == "/fake/clone/o__r"
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
    assert spawns[0]["cwd"] == "/fake/clone/o__r"
    prompt = spawns[0]["initial_prompt"]
    assert prompt == build_conflict_fix_prompt("o/r", pr)
    assert prompt != build_pr_fix_prompt("o/r", pr)
    assert "conflict" in prompt.lower()

    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert st.get("attempts") == 1


@pytest.mark.asyncio
async def test_failing_ci_no_driver_clone_failure_does_not_consume_attempt(monkeypatch, tmp_path):
    """failing_ci + auto + NO live driver + ensure_clone raises CloneError →
    - a spawn_failed / error event is emitted
    - spawn_session is NOT called
    - the attempt is NOT consumed (total_attempts / attempts stay 0)
    - took_action False → falls through to notify (target re-arms for a retry)
    """
    from ccswitch import gitrepos
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
    clone_calls = _patch_ensure_clone(monkeypatch, raises=gitrepos.CloneError("boom"))

    took = await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert took is False           # fell through to notify
    assert len(clone_calls) == 1   # the clone was attempted
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
async def test_merge_conflict_auto_live_driver_injects_conflict_prompt(monkeypatch, tmp_path):
    """merge_conflict + auto + auto_resolve_conflicts=True + LIVE driver →
    inject the CONFLICT prompt via send_prompt, attempt consumed, no spawn."""
    from server.sessions.pr_fix import build_conflict_fix_prompt
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
    assert took is True
    assert spawns == []  # live driver → inject, not spawn
    assert len(sent) == 1
    assert sent[0] == build_conflict_fix_prompt("o/r", pr)
    assert "conflict" in sent[0].lower()

    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert st.get("attempts") == 1


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
async def test_async_inject_loop_gives_up_and_emits_gave_up(monkeypatch, tmp_path):
    """End-to-end async-inject regression: each failing poll carries a DIFFERENT
    head_sha (the agent's fix pushed a new commit that stays red). apply_rearm
    keeps re-arming the per-head guard, but after `max_attempts` (3) injections
    the target row is sticky-gave_up and a `gave_up` event is emitted; no further
    inject fires."""
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

    # max_attempts (default 3) injections, then give-up halts further injects.
    assert len(sent) == 3

    st = await orch._pr_doctor_ctx.get_target_state("o/r#7")
    assert st.get("gave_up") is True
    assert st.get("total_attempts") == 3

    events = await orch._pr_doctor_ctx.list_events(target="o/r#7")
    assert any(e["kind"] == "gave_up" for e in events)


@pytest.mark.asyncio
async def test_failing_ci_one_attempt_per_head(monkeypatch, tmp_path):
    """Two failing polls on the SAME head → exactly one inject (guard holds)."""
    await _init_pr_doctor_db(tmp_path)
    await _set_mode(monkeypatch, "auto")

    pr = _pr(checks="failing", head_sha="sha1")
    orch = _build_orch(pr, has_driver=True)
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]

    await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    await orch._pr_doctor_step("o/r", "feat/x", pr, rec=orch._records["sess1"])
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_external_push_re_arms_second_attempt(monkeypatch, tmp_path):
    """An external push (new head_sha) re-arms → a second inject fires."""
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
    assert len(sent) == 2


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
    assert any(e["kind"] == "terminal" for e in events)


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

    orch._gh_run = fake_gh_run  # type: ignore[assignment]

    result = await orch.automation_trigger("pr-doctor", "o/r#7", "out_of_date")
    assert result["status"] == "updated"
    assert len(gh_calls) == 1
    assert "update-branch" in " ".join(gh_calls[0])


@pytest.mark.asyncio
async def test_pr_doctor_fix_failing_ci_injects_live_driver(monkeypatch, tmp_path):
    """automation_trigger(failing_ci) with a live owning driver → the shared session
    executor injects via request_pr_fix (send_prompt), result status is 'sent'."""
    await _init_pr_doctor_db(tmp_path)

    pr = _pr(checks="failing", head_sha="sha1")
    orch = _build_orch(pr, has_driver=True)
    await _pr_pairs_stub(orch, [("o/r", "feat/x")])
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]

    result = await orch.automation_trigger("pr-doctor", "o/r#7", "failing_ci")
    assert result["status"] == "sent"
    assert len(sent) == 1
    assert "gh pr checkout 7" in sent[0]


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
