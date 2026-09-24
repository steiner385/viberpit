# server/tests/test_pr_doctor_primary_only.py
"""PR Doctor is fleet-global, so ONLY the primary daemon may drive it (#2106).

#919 forbids a worker from driving a fleet-global automation against the one
shared DB, and ``start()`` enforces that for every automation that owns a loop
task (``_PRIMARY_ONLY_LOOP_ATTRS``, pinned by ``test_primary_lease``). PR Doctor
owns no loop task: it hangs off the PR-status poller — which every daemon runs
for its own sessions — via ``on_change``/``on_cycle``. It was therefore invisible
to that gate and ran on workers, where the spawn reached
``_automation_create_session`` → ``create_via_edge(127.0.0.1:8787)`` with no edge
co-located, producing the production WARNING ``automation_create_session: edge
unreachable`` and pinning every repair session to the racing host instead of the
capacity-ranked placement.

These tests pin the BEHAVIOUR (a worker takes no pr-doctor action), which is what
the task-attribute gate could never see, plus the two things the fix must NOT
break: the primary still acts, and a worker still raises its own sessions'
"PR broke" push.
"""
from __future__ import annotations

import asyncio

from server.tests.test_pr_doctor_sweep import (  # noqa: F401 — fixture import
    github_identity,
    _build_orch,
    _init_pr_doctor_db,
    _pr,
    _set_config,
    _set_mode,
)


def _failing_orch():
    """An orchestrator whose one tracked PR is unambiguously actionable."""
    pr = _pr(merge_state_status="BEHIND", checks="passing")
    return _build_orch({("o/r", "feat/x"): pr}, [("o/r", "feat/x")])


def _record_gh(monkeypatch, orch, calls):
    """Replace the mechanical boundary so a dispatched action is observable."""
    import types

    from server.tests.test_pr_doctor_step import _mechanical_boundary

    _mechanical_boundary(monkeypatch, orch)
    orch._gh_run = lambda args: (calls.append(list(args)),  # type: ignore[assignment]
                                 types.SimpleNamespace(returncode=0))[1]


# ---------------------------------------------------------------------------
# The sweep (on_cycle)
# ---------------------------------------------------------------------------


def test_worker_sweep_takes_no_pr_doctor_action(monkeypatch, tmp_path):
    """The regression. Same orchestrator + enabled automation + actionable PR as
    the primary case below — only VIBERPIT_DAEMON_ROLE differs."""
    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "worker")

    async def run():
        await _init_pr_doctor_db(tmp_path)
        await _set_mode(monkeypatch, "auto")
        await _set_config({"auto_update_out_of_date": True})

        orch = _failing_orch()
        gh_calls: list[list[str]] = []
        _record_gh(monkeypatch, orch, gh_calls)

        await orch._pr_doctor_sweep()

        assert gh_calls == [], f"worker dispatched a pr-doctor action: {gh_calls}"
        assert await orch._pr_doctor_ctx.list_events(target="o/r#7") == []
        assert await orch._pr_doctor_ctx.list_target_states() == {}

    asyncio.run(run())


def test_primary_sweep_still_acts(monkeypatch, tmp_path):
    """The mirror: the gate must not disable PR Doctor outright. Without this,
    ``return`` at the top of the sweep would pass the worker test forever."""
    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)

    async def run():
        await _init_pr_doctor_db(tmp_path)
        await _set_mode(monkeypatch, "auto")
        await _set_config({"auto_update_out_of_date": True})

        orch = _failing_orch()
        gh_calls: list[list[str]] = []
        _record_gh(monkeypatch, orch, gh_calls)

        await orch._pr_doctor_sweep()

        assert len(gh_calls) == 1, gh_calls
        assert "update-branch" in " ".join(gh_calls[0])

    asyncio.run(run())


# ---------------------------------------------------------------------------
# The checks-transition callback (on_change)
# ---------------------------------------------------------------------------


def test_worker_on_pr_change_takes_no_pr_doctor_action(monkeypatch, tmp_path):
    """The other entry point: a passing→failing transition on a worker must not
    reach ``_pr_doctor_step`` or emit onto the pr-doctor activity feed."""
    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "worker")

    async def run():
        import server.sessions.orchestrator as orch_mod

        await _init_pr_doctor_db(tmp_path)
        await _set_mode(monkeypatch, "auto")

        pr = _pr(checks="failing", merge_state_status="BLOCKED")
        orch = _build_orch({("o/r", "feat/x"): pr}, [("o/r", "feat/x")])

        steps: list[tuple[str, str]] = []

        async def fake_step(repo, branch, info, *, rec=None):
            steps.append((repo, branch))
            return False

        orch._pr_doctor_step = fake_step  # type: ignore[method-assign]

        async def fake_notify_event(*a, **kw):
            return None

        monkeypatch.setattr(orch_mod.push_mod, "notify_event", fake_notify_event)

        await orch._on_pr_change("o/r", "feat/x", "passing", "failing")

        assert steps == [], f"worker ran _pr_doctor_step: {steps}"
        assert await orch._pr_doctor_ctx.list_events(target="o/r#7") == []

    asyncio.run(run())


def test_worker_still_pushes_its_own_sessions_pr_broke_alert(monkeypatch, tmp_path):
    """``_on_pr_change`` carries a PER-HOST duty alongside the fleet-global one:
    the push is keyed off ``self._records``, which holds a worker's OWN sessions
    and not the primary's, so the worker is the only daemon that can raise it.
    Skipping the whole callback on a worker (or never wiring ``set_on_change``)
    would silently drop every worker session's "PR broke" notification."""
    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "worker")

    async def run():
        import server.sessions.orchestrator as orch_mod

        await _init_pr_doctor_db(tmp_path)
        await _set_mode(monkeypatch, "auto")

        pr = _pr(checks="failing", merge_state_status="BLOCKED")
        orch = _build_orch({("o/r", "feat/x"): pr}, [("o/r", "feat/x")])

        pushed: list[dict] = []

        async def fake_notify_event(event, title, body, **kw):
            pushed.append({"event": event, "body": body, **kw})

        monkeypatch.setattr(orch_mod.push_mod, "notify_event", fake_notify_event)

        await orch._on_pr_change("o/r", "feat/x", "passing", "failing")

        assert [p["event"] for p in pushed] == ["pr_broke"], pushed
        assert pushed[0]["session_id"] == "sess1"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def test_drives_fleet_automations_tracks_the_daemon_role(monkeypatch):
    """One named predicate for "may this daemon drive fleet-global work", so the
    next automation that rides shared per-host machinery has somewhere to hang
    its guard instead of inventing a parallel role check."""
    from server.sessions.orchestrator import drives_fleet_automations

    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)
    assert drives_fleet_automations() is True
    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "primary")
    assert drives_fleet_automations() is True
    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "worker")
    assert drives_fleet_automations() is False
