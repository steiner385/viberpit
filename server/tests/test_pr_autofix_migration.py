"""pr_autofix mode + loop-guard sourced from the registry; AppSettings field gone.

Task 9 — behavior-preserving migration of pr_autofix off ``AppSettings`` and the
in-memory ``_pr_fix_attempts`` set onto the automation registry:
  * mode reads the ``pr-doctor`` Automation row's ``mode`` column;
  * the auto-fix loop guard lives in durable ``automation_target_state`` keyed by
    ``f"{repo}#{number}"``, with the SAME invariants the old in-memory set had.

Follows the suite's sync ``asyncio.run(run())`` + ``tmp_path`` file-DB pattern
(no ``asyncio_mode=auto``); see test_automation_models.py.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from server.github.pr_status import PullRequestInfo
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


# --------------------------------------------------------------------------- #
# Helpers                                                                       #
# --------------------------------------------------------------------------- #

def _pr(number=7, state="open", checks="failing"):
    return PullRequestInfo(
        number=number, url=f"https://x/pull/{number}", state=state, checks=checks, title="t")


def _make_record(session_id="sess1", repo="o/r", branch="feat/x"):
    return SessionRecord(
        id=session_id, name="test-session", account="acct-a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="",
        cols=80, rows=24, cwd="/tmp", repo=repo, branch=branch,
        authored_branches=[branch])


class _StubPoller:
    """cached() returns a fixed PR for the known (repo, branch)."""

    def __init__(self, pr: PullRequestInfo, repo="o/r", branch="feat/x"):
        self._pr, self._repo, self._branch = pr, repo, branch

    def cached(self, repo: str, branch: str):
        return self._pr if (repo, branch) == (self._repo, self._branch) else None


async def _init_db(tmp_path, name: str) -> None:
    from server.db import init_db
    from server.automations.registry import registry
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / name}")
    await registry.ensure_rows()
    # PR Doctor's action paths (_on_pr_change → _pr_doctor_step) now gate on the
    # enabled flag; ensure_rows seeds enabled=False, so enable it here. The mode
    # column (asserted by the mode-read tests) is independent of this flag.
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        await db.execute(
            sa_update(Automation).where(Automation.id == "pr-doctor").values(enabled=True))


def _build_orch(pr: PullRequestInfo, *, has_driver: bool = True):
    """Build a minimal orch + a no-network notify shim. ``pushed`` collects the
    notify titles; the caller is responsible for swapping notify in (done here on
    the module so the module-level ``push_mod.notify`` call in _on_pr_change hits
    the shim). The original is restored when the process-scoped engine is rebuilt
    by the next test's init_db — but to avoid leaking across files we restore it
    via the returned ``restore`` callable."""
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext

    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = {"sess1": _make_record()}
    orch._pushed_pr = set()
    orch._pr_doctor_ctx = AutomationContext("pr-doctor")
    orch._pr_poller = _StubPoller(pr)  # type: ignore[assignment]
    orch._drivers = {"sess1": object()} if has_driver else {}  # type: ignore[dict-item]
    sent: list[str] = []
    pushed: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    async def fake_notify(title, body, *, url=None):
        pushed.append(title)

    orch.send_prompt = fake_send  # type: ignore[method-assign, assignment]

    # No-driver SessionFix now spawns a fresh de-duped session; record spawns so
    # no real create_session runs. Exposed on the orch for the tests that inspect.
    import types as _types
    spawns: list[dict] = []
    _seen: dict[str, object] = {}

    async def fake_spawn(*, origin_ref, dedupe_key=None, **kwargs):
        key = dedupe_key or origin_ref
        if key in _seen:
            return _seen[key]
        rec = _types.SimpleNamespace(id="spawned-1")
        _seen[key] = rec
        spawns.append({"origin_ref": origin_ref, "dedupe_key": dedupe_key, **kwargs})
        return rec

    orch._pr_doctor_ctx.spawn_session = fake_spawn  # type: ignore[method-assign]
    orch._test_spawns = spawns  # type: ignore[attr-defined]

    # Resolve the clone cwd off a fake so the no-driver spawn path runs without
    # real git/gh; restored alongside notify below.
    def _fake_ensure_clone(repo_root, name_with_owner, url):
        return "/fake/clone/o__r"

    _orig_ensure_clone = orch_mod.gitrepos.ensure_clone
    orch_mod.gitrepos.ensure_clone = _fake_ensure_clone  # type: ignore[attr-defined]

    _orig_notify = orch_mod.push_mod.notify
    orch_mod.push_mod.notify = fake_notify  # type: ignore[attr-defined]

    def restore():
        orch_mod.push_mod.notify = _orig_notify  # type: ignore[attr-defined]
        orch_mod.gitrepos.ensure_clone = _orig_ensure_clone  # type: ignore[attr-defined]

    return orch, sent, pushed, restore


async def _set_mode(mode: str) -> None:
    """Set the pr-doctor registry row's mode."""
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        row = (await db.execute(
            select(Automation).where(Automation.id == "pr-doctor"))).scalar_one()
        row.mode = mode


# --------------------------------------------------------------------------- #
# Mode read from the registry row                                              #
# --------------------------------------------------------------------------- #

def test_mode_read_from_registry_row(tmp_path):
    async def run():
        await _init_db(tmp_path, "mode.db")
        orch, _sent, _pushed, restore = _build_orch(_pr())
        # Default: pr-doctor row exists with mode=NULL → 'propose'.
        assert await orch._pr_autofix_mode() == "propose"
        await _set_mode("auto")
        assert await orch._pr_autofix_mode() == "auto"
        # Unknown/garbage mode falls back to 'propose'.
        await _set_mode("bogus")
        assert await orch._pr_autofix_mode() == "propose"

        restore()

    asyncio.run(run())


def test_mode_propose_when_no_pr_doctor_row(tmp_path):
    async def run():
        from server.db import init_db, db_session
        from server.db.models import Automation
        from sqlalchemy import delete
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'norow.db'}")
        # Deliberately do NOT ensure_rows — no pr-doctor row at all.
        async with db_session() as db:
            await db.execute(delete(Automation))
        orch, _sent, _pushed, restore = _build_orch(_pr())
        assert await orch._pr_autofix_mode() == "propose"

        restore()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# AppSettings field gone                                                       #
# --------------------------------------------------------------------------- #

def test_appsettings_no_longer_has_pr_autofix_mode():
    from server.db.models import AppSettings
    assert not hasattr(AppSettings, "pr_autofix_mode")


# --------------------------------------------------------------------------- #
# Loop-guard invariants on the durable store                                  #
# --------------------------------------------------------------------------- #

def test_one_attempt_per_failure_event(tmp_path):
    """failing → (auto-attempt) → failing again ⇒ exactly ONE injection."""
    async def run():
        await _init_db(tmp_path, "one.db")
        await _set_mode("auto")
        orch, sent, _pushed, restore = _build_orch(_pr())

        await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
        assert len(sent) == 1
        # Second failing without recovery → guard holds, no second injection.
        await orch._on_pr_change("o/r", "feat/x", "failing", "failing")
        assert len(sent) == 1

        restore()

    asyncio.run(run())


def test_no_rearm_on_pending(tmp_path):
    """failing → pending → failing ⇒ still NO second injection (no re-arm on pending)."""
    async def run():
        await _init_db(tmp_path, "pending.db")
        await _set_mode("auto")
        orch, sent, _pushed, restore = _build_orch(_pr())

        await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
        assert len(sent) == 1
        await orch._on_pr_change("o/r", "feat/x", "failing", "pending")
        await orch._on_pr_change("o/r", "feat/x", "pending", "failing")
        assert len(sent) == 1  # pending must NOT re-arm

        restore()

    asyncio.run(run())


def test_rearm_on_passing(tmp_path):
    """failing → passing → failing ⇒ a NEW injection (the genuine green re-armed)."""
    async def run():
        await _init_db(tmp_path, "passing.db")
        await _set_mode("auto")
        orch, sent, _pushed, restore = _build_orch(_pr())

        await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
        assert len(sent) == 1
        await orch._on_pr_change("o/r", "feat/x", "failing", "passing")
        await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
        assert len(sent) == 2

        restore()

    asyncio.run(run())


def test_no_driver_spawns_deduped(tmp_path):
    """No live driver ⇒ spawn a fresh de-duped fix session (recovery took action).

    Supersedes the old no-driver→notify+re-arm behaviour: the SessionFix branch
    now spawns when the owning driver is gone, consuming the per-head attempt.
    A second failing poll on the same head de-dups (guard holds), so no second
    spawn and no injection even once a driver appears on the same head."""
    async def run():
        await _init_db(tmp_path, "drain.db")
        await _set_mode("auto")
        orch, sent, pushed, restore = _build_orch(_pr(), has_driver=False)

        await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
        assert sent == []  # no live driver → spawn, not inject
        assert pushed == []  # recovery fired → no break notification
        assert len(orch._test_spawns) == 1  # type: ignore[attr-defined]
        assert orch._test_spawns[0]["origin_ref"] == "o/r#7"  # type: ignore[attr-defined]

        # Same head, second poll → per-head guard holds: no second spawn.
        await orch._on_pr_change("o/r", "feat/x", "failing", "failing")
        assert len(orch._test_spawns) == 1  # type: ignore[attr-defined]

        restore()

    asyncio.run(run())


def test_attempted_flag_persisted_in_target_state(tmp_path):
    """The loop guard is the durable automation_target_state row, not an in-memory
    set: after a real injection the pr-doctor target carries attempted=True."""
    async def run():
        await _init_db(tmp_path, "state.db")
        await _set_mode("auto")
        orch, sent, _pushed, restore = _build_orch(_pr())

        await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
        assert len(sent) == 1
        from server.db import db_session
        from server.db.models import AutomationTargetState
        async with db_session() as db:
            row = (await db.execute(select(AutomationTargetState).where(
                AutomationTargetState.automation_id == "pr-doctor",
                AutomationTargetState.target == "o/r#7"))).scalar_one()
            assert row.state.get("attempted") is True

        restore()

    asyncio.run(run())


def test_emits_detected_and_attempted_events(tmp_path):
    """The activity feed populates: a failure entry emits ``detected`` and a real
    auto-injection emits ``attempted``."""
    async def run():
        await _init_db(tmp_path, "events.db")
        await _set_mode("auto")
        orch, sent, _pushed, restore = _build_orch(_pr())

        await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
        kinds = {e["kind"] for e in await orch._pr_doctor_ctx.list_events()}
        assert "detected" in kinds
        assert "attempted" in kinds

        restore()

    asyncio.run(run())


def test_no_pr_fix_attempts_attribute(tmp_path):
    """The old in-memory loop-guard set is gone — a fully constructed orchestrator
    carries no ``_pr_fix_attempts`` (no parallel state)."""
    async def run():
        from unittest.mock import MagicMock, patch
        await _init_db(tmp_path, "attr.db")
        from server.sessions.orchestrator import SessionOrchestrator
        fake_sdk = MagicMock()
        with patch("server.sessions.orchestrator.real_sdk_factory", fake_sdk), \
             patch("server.sessions.orchestrator.SuggestionService"), \
             patch("server.sessions.orchestrator.UsagePoller"), \
             patch("server.github.pr_poller.PrStatusPoller"):
            orch = SessionOrchestrator(sdk_factory=fake_sdk)
        assert not hasattr(orch, "_pr_fix_attempts")
        assert hasattr(orch, "_pr_doctor_ctx")

    asyncio.run(run())
