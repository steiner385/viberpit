"""Reading an automation's persisted (mode, config) off its ``Automation`` row.

Every control loop needs the same three things — the row, its config merged onto the
schema defaults, and the *effective* mode — and every one of them used to spell it out
again in the orchestrator (issue #985: nine near-identical ``_<id>_read_config``
helpers). One copy means one place where "a bad row can never act" is true.

The invariant those copies shared, and that this preserves: **any** failure resolves to
``off`` plus pure defaults. A missing row, an unreadable DB, a config that no longer
validates against the schema — none of them may leave an automation running on
half-trusted settings, because for several of these loops "act" means writing to GitHub.

Mode resolution differs between automations, so it stays explicit rather than inferred:

* Multi-mode automations (``propose``/``auto``, ``dry_run``/``apply``) require the row to
  name one of them; an enabled row with no mode is ``off``.
* Two-mode automations (off + one active mode) hide the mode ``<select>`` in the UI, so
  their toggle enables the row with ``mode`` left NULL. They pass ``default_mode`` to
  resolve that to the active mode — without it an enabled row would be stuck ``off``
  forever.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import select

from server.db import db_session
from server.db.models import Automation as AutomationRow

from .base import ConfigSchema, validate_config

log = logging.getLogger(__name__)


async def read_automation_config(automation_id: str, schema: ConfigSchema, *,
                                 modes: Sequence[str],
                                 default_mode: str | None = None,
                                 log_key: str | None = None) -> tuple[str, dict]:
    """``(effective_mode, validated_config)`` for *automation_id*.

    *modes* are the active (non-``off``) modes the row may carry. *default_mode* is the
    mode an ENABLED row with no explicit mode resolves to — leave it ``None`` for
    automations whose UI always writes an explicit mode.
    """
    enabled, mode, raw = False, None, {}
    try:
        async with db_session() as db:
            row = (await db.execute(select(AutomationRow).where(
                AutomationRow.id == automation_id))).scalar_one_or_none()
            if row is not None:
                enabled, mode, raw = bool(row.enabled), row.mode, dict(row.config or {})
    except Exception:
        log.debug("%s.config.load_failed", log_key or automation_id, exc_info=True)
    try:
        config = validate_config(schema, raw)
    except ValueError:
        config = validate_config(schema, {})
    if not enabled:
        return "off", config
    if mode is not None and mode in modes:
        return mode, config
    return default_mode or "off", config


async def read_effective_config(automation_id: str) -> dict:
    """The validated config for a REGISTERED automation, schema resolved for you.

    ``read_automation_config`` above is for a control loop that already holds its
    own schema and mode vocabulary. The spawn helper does not — it knows only an
    ``automation_id`` — so it needs the registry's answer for both, including the
    engine fields the origin's audited ceiling adds (Phase 7, Task 7.1).

    Same invariant as its sibling: any failure resolves to pure defaults, which
    for the engine fields means ``claude_code``. An unregistered id (a caller
    passing an ad-hoc origin) answers ``{}`` rather than raising into a spawn.
    """
    from .engine_policy import effective_config_schema
    from .registry import registry

    try:
        automation = registry.get(automation_id)
    except KeyError:
        return {}
    _mode, config = await read_automation_config(
        automation_id, effective_config_schema(automation), modes=automation.modes)
    return config
