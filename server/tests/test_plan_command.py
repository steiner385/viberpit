"""D-16a `/plan [on|off]`: the app command behind the ModePicker's plan toggle.

`/plan` runs ``set_permission_mode`` — plan is a driver overlay whose ``default`` returns
to the pre-plan mode (Review Focus 4). It changes nothing when the session is already in
the asked-for state: `off` outside plan must never send ``default``, which would drop a
full-auto or acceptEdits session to the prompting ``default`` mode. It is APP_ONLY, so
it never reaches the CLI. The fixture is ``test_output_style``'s real orchestrator on
the fake CLI.
"""
from __future__ import annotations

import pytest

from server.sessions.commands.catalog import APP_COMMAND, APP_COMMAND_REFUSED
from server.tests.test_output_style import _Spy
from server.tests.test_output_style import env as env  # noqa: F401 — fixture


_U = "u@example.com"     # a person: leaving plan widens the mode, human-only (#3291)


def _mode(env) -> str:
    return env.orch._drivers[env.rec.id].mode


class _ModeCalls:
    """Records every set_permission_mode the driver is handed."""

    def __init__(self, driver) -> None:
        self.calls: list[str] = []
        real = driver.set_permission_mode

        async def spy(mode):
            self.calls.append(mode)
            return await real(mode)
        driver.set_permission_mode = spy


# Review Focus 4
@pytest.mark.asyncio
async def test_plan_off_after_on_returns_to_accept_edits(env) -> None:
    await env.orch.set_permission_mode(env.rec.id, "acceptEdits")
    assert _mode(env) == "acceptEdits"
    on = await env.orch.run_command(env.rec.id, "plan", "on", identity=_U)
    assert on is not None and on["render"] == "notice" and on["data"]["text"] == "Plan mode on"
    assert _mode(env) == "plan"
    off = await env.orch.run_command(env.rec.id, "plan", "off", identity=_U)
    assert off is not None and off["data"]["text"] == "Plan mode off"
    assert _mode(env) == "acceptEdits"


@pytest.mark.asyncio
@pytest.mark.parametrize("start", ["full-auto", "acceptEdits"])
async def test_plan_off_outside_plan_changes_nothing(env, start) -> None:
    """Fix round 1, item 1: `/plan off` on a session that is not in plan is a no-op
    notice — never a `default` that would drop it to prompting on every tool."""
    await env.orch.set_permission_mode(env.rec.id, start)
    calls = _ModeCalls(env.orch._drivers[env.rec.id])
    out = await env.orch.run_command(env.rec.id, "plan", "off")
    assert out is not None and out["data"]["text"] == "Plan mode is already off"
    assert _mode(env) == start and calls.calls == []


@pytest.mark.asyncio
async def test_plan_on_inside_plan_changes_nothing(env) -> None:
    await env.orch.set_permission_mode(env.rec.id, "acceptEdits")
    await env.orch.run_command(env.rec.id, "plan", "on")
    calls = _ModeCalls(env.orch._drivers[env.rec.id])
    out = await env.orch.run_command(env.rec.id, "plan", "on")
    assert out is not None and out["data"]["text"] == "Plan mode is already on"
    assert calls.calls == []
    await env.orch.run_command(env.rec.id, "plan", "off", identity=_U)   # overlay intact
    assert _mode(env) == "acceptEdits"


@pytest.mark.asyncio
async def test_bare_plan_toggles(env) -> None:
    await env.orch.run_command(env.rec.id, "plan", "", identity=_U)
    assert _mode(env) == "plan"
    await env.orch.run_command(env.rec.id, "plan", "", identity=_U)
    assert _mode(env) == "full-auto"
    bad = await env.orch.run_command(env.rec.id, "plan", "maybe")
    assert bad is not None and "Usage: /plan" in bad["data"]["text"]
    assert _mode(env) == "full-auto"


@pytest.mark.asyncio
async def test_plan_off_from_the_system_identity_is_refused_as_a_notice(env) -> None:
    """#3291: leaving plan sends ``default``, which widens the mode, so an automated
    caller gets a notice and the session stays in plan."""
    await env.orch.run_command(env.rec.id, "plan", "on")      # tightening: open to all
    assert _mode(env) == "plan"
    out = await env.orch.run_command(env.rec.id, "plan", "off")
    assert out is not None and out["render"] == "notice"
    assert out["data"]["text"].startswith("Plan mode unchanged")
    assert _mode(env) == "plan"


@pytest.mark.asyncio
async def test_plan_on_a_detached_session_is_a_notice(env) -> None:
    env.orch._drivers.pop(env.rec.id)
    out = await env.orch.run_command(env.rec.id, "plan", "on")
    assert out is not None and out["render"] == "notice"
    assert "isn't live" in out["data"]["text"]


@pytest.mark.asyncio
async def test_plan_typed_as_a_prompt_never_reaches_the_cli(env) -> None:
    spy = _Spy(env.orch._drivers[env.rec.id])
    assert await env.orch.send_prompt(env.rec.id, "/plan on", command_origin="composer",
                                      identity="u@example.com") == APP_COMMAND
    assert spy.prompts == [] and _mode(env) == "plan"


@pytest.mark.asyncio
async def test_an_automated_plan_off_never_leaves_plan_mode(env) -> None:
    """S1: exiting plan mode is a privilege change — automated text cannot do it."""
    await env.orch.run_command(env.rec.id, "plan", "on", identity="u@example.com")
    for kw in ({}, {"origin": {"kind": "autopilot"}}):
        assert await env.orch.send_prompt(env.rec.id, "/plan off", **kw) == APP_COMMAND_REFUSED
    assert _mode(env) == "plan"


@pytest.mark.asyncio
async def test_plan_is_audited_as_a_permission_mode_change(env) -> None:
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AuditEvent
    await env.orch.run_command(env.rec.id, "plan", "on", identity="u@example.com")
    await env.orch.run_command(env.rec.id, "plan", "on")             # no-op: not audited
    async with db_session() as db:
        rows = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "permission_mode.change"))).scalars().all()
    assert [(r.identity, r.detail["mode"]) for r in rows] == [("u@example.com", "plan")]


@pytest.mark.asyncio
async def test_bare_plan_reads_a_deferred_switch_not_the_live_mode(env) -> None:
    """Codex defers a mid-turn switch (`_pending_profile`): live `mode` is still the old
    one, so a bare `/plan` must toggle from where the session is heading (#3294)."""
    driver = env.orch._drivers[env.rec.id]
    driver.requested_mode = "plan"          # deferred switch into plan; live mode is not plan
    calls = _ModeCalls(driver)
    await env.orch.run_command(env.rec.id, "plan", "", identity=_U)
    assert calls.calls == ["default"]
