"""``read_automation_config`` — the one reader behind every ``_<id>_read_config`` seam.

Extracted from nine near-identical orchestrator copies (issue #985), so the shared
invariant is asserted once, here: **any** failure resolves to ``off`` plus pure schema
defaults, because for several of these loops "act" means writing to GitHub.

Harness mirrors ``test_autopilot_orchestrator_seams``: a real SQLite DB with the
automation rows registry-seeded, driven through the orchestrator's own seams so the
test covers the wiring, not just the helper.
"""
from __future__ import annotations

import asyncio

from server.automations.config_store import read_automation_config


def _bare_orch():
    import server.sessions.orchestrator as orch_mod
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._draining = False
    return orch


async def _init_db(tmp_path) -> None:
    from server.automations.registry import registry
    from server.db import init_db
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'cfg.db'}")
    await registry.ensure_rows()


async def _set_row(automation_id: str, *, enabled: bool, mode: str | None,
                   config: dict | None = None) -> None:
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Automation
    values: dict = {"enabled": enabled, "mode": mode}
    if config is not None:
        values["config"] = config
    async with db_session() as db:
        await db.execute(sa_update(Automation).where(Automation.id == automation_id)
                         .values(**values))


def test_a_disabled_or_modeless_row_never_resolves_to_a_writing_mode(tmp_path):
    """gh-reconcile is the sharp case: ``apply`` writes to GitHub, so an enabled row with
    no mode must resolve to ``off``, NOT to the first active mode. It passes no
    ``default_mode`` for exactly that reason."""
    async def run():
        await _init_db(tmp_path)
        orch = _bare_orch()
        assert (await orch._gh_reconcile_read_config())[0] == "off"     # seeded disabled
        await _set_row("gh-reconcile", enabled=True, mode=None)
        assert (await orch._gh_reconcile_read_config())[0] == "off"
        await _set_row("gh-reconcile", enabled=True, mode="nonsense")
        assert (await orch._gh_reconcile_read_config())[0] == "off"
        await _set_row("gh-reconcile", enabled=False, mode="apply")
        assert (await orch._gh_reconcile_read_config())[0] == "off"     # toggle beats mode
        await _set_row("gh-reconcile", enabled=True, mode="dry_run")
        assert (await orch._gh_reconcile_read_config())[0] == "dry_run"
    asyncio.run(run())


def test_a_two_mode_automation_resolves_an_enabled_row_with_no_mode(tmp_path):
    """archive-audit and cicd-doctor hide the mode ``<select>`` in the UI, so their toggle
    enables the row with ``mode`` left NULL. Without ``default_mode`` such a row would be
    enabled-but-stuck-off forever."""
    async def run():
        await _init_db(tmp_path)
        orch = _bare_orch()
        await _set_row("archive-audit", enabled=True, mode=None)
        assert (await orch._archaudit_read_config())[0] == "act"
        await _set_row("cicd-doctor", enabled=True, mode=None)
        assert (await orch._cicd_read_config())[0] == "propose"
        await _set_row("cicd-doctor", enabled=False, mode=None)
        assert (await orch._cicd_read_config())[0] == "off"
    asyncio.run(run())


def test_an_invalid_config_falls_back_to_pure_defaults(tmp_path):
    """A row whose stored config no longer validates against the schema must not leave the
    loop running on half-trusted settings — it gets the schema defaults, whole."""
    async def run():
        await _init_db(tmp_path)
        orch = _bare_orch()
        await _set_row("gh-reconcile", enabled=True, mode="apply",
                       config={"max_mutations_per_cycle": "not-an-int"})
        mode, config = await orch._gh_reconcile_read_config()
        assert mode == "apply"
        assert config["max_mutations_per_cycle"] == 50 and config["migrate"] is False
        # A VALID override survives, so the fallback is not just ignoring stored config.
        await _set_row("gh-reconcile", enabled=True, mode="apply",
                       config={"max_mutations_per_cycle": 7})
        assert (await orch._gh_reconcile_read_config())[1]["max_mutations_per_cycle"] == 7
    asyncio.run(run())


def test_an_engine_capable_automation_reads_its_stored_config_whole(tmp_path):
    """The engine fields (``engine`` / ``codex_model`` / ``codex_reasoning_effort``) live on
    the ENGINE-POLICY half of the schema, so a loop that validates against the automation's
    base ``config_schema`` alone raises ``unknown config key: 'engine'`` on its OWN persisted
    row and silently degrades to pure defaults.

    That is not a theoretical drift: it took the backlog runner down for two days. Defaults
    mean ``enabled_repos: []``, and ``query_candidates`` returns ``[]`` on that before it
    ever shells ``gh`` — so the card read "0 eligible" over a 156-issue backlog, with no
    failing log line anywhere, while the row in the UI still showed 14 repos.
    """
    async def run():
        await _init_db(tmp_path)
        orch = _bare_orch()
        await _set_row("backlog-runner", enabled=True, mode="auto", config={
            "enabled_repos": ["steiner385/viberpit"], "max_concurrent": 6,
            "engine": "claude_code", "codex_model": "", "codex_reasoning_effort": ""})
        mode, config = await orch._backlog_read_config()
        assert mode == "auto"
        assert config["enabled_repos"] == ["steiner385/viberpit"]
        assert config["max_concurrent"] == 6
        assert config["engine"] == "claude_code"
        # PR Doctor persists the same fields and had its own hand-rolled copy of the read.
        await _set_row("pr-doctor", enabled=True, mode="auto", config={
            "enabled_repos": ["steiner385/viberpit"], "auto_resolve_conflicts": True,
            "engine": "claude_code", "codex_model": "", "codex_reasoning_effort": ""})
        pr_config = await orch._pr_doctor_config()
        assert pr_config["enabled_repos"] == ["steiner385/viberpit"]
        assert pr_config["auto_resolve_conflicts"] is True
    asyncio.run(run())


def test_an_unreadable_db_resolves_to_off_and_defaults(tmp_path):
    """The DB read is wrapped because a control loop must survive a transient DB failure
    without acting on stale or absent config."""
    async def run():
        from server.automations.gh_reconcile import GhReconcile

        class Unseeded(GhReconcile):        # a registered schema, but no row to read
            id = "no-such-automation"

        await _init_db(tmp_path)
        mode, config = await read_automation_config(
            Unseeded, modes=("dry_run", "apply"))
        assert mode == "off" and config["migrate"] is False
    asyncio.run(run())
