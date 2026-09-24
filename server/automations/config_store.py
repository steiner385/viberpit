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

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select

from server.db import db_session
from server.db.models import Automation as AutomationRow

from .base import retired_config_keys_of, retired_modes_of, validate_config

log = logging.getLogger(__name__)


async def read_automation_config(automation: Any, *,
                                 modes: Sequence[str],
                                 default_mode: str | None = None,
                                 log_key: str | None = None) -> tuple[str, dict]:
    """``(effective_mode, validated_config)`` for *automation*.

    *automation* is the automation itself (class or instance), not a schema — the schema
    is resolved here via ``effective_config_schema``, so a loop cannot read through the
    base ``config_schema`` while the API validates and persists through the engine-policy
    one. That split was a real outage: the backlog runner's own row carries ``engine`` /
    ``codex_model`` / ``codex_reasoning_effort``, ``validate_config`` rejects unknown keys,
    and the loop degraded to ``enabled_repos: []`` — a silent "0 eligible" over a full
    backlog for two days.

    *modes* are the active (non-``off``) modes the row may carry. *default_mode* is the
    mode an ENABLED row with no explicit mode resolves to — leave it ``None`` for
    automations whose UI always writes an explicit mode.
    """
    from .engine_policy import effective_config_schema

    automation_id = getattr(automation, "id", None) or str(automation)
    schema = effective_config_schema(automation)
    enabled, mode, raw = False, None, {}
    try:
        async with db_session() as db:
            row = (await db.execute(select(AutomationRow).where(
                AutomationRow.id == automation_id))).scalar_one_or_none()
            if row is not None:
                enabled, mode, raw = bool(row.enabled), row.mode, dict(row.config or {})
    except Exception:
        log.debug("%s.config.load_failed", log_key or automation_id, exc_info=True)
    retired = retired_config_keys_of(automation)
    try:
        config = validate_config(schema, raw, retired=retired)
    except ValueError as exc:
        # Fail SAFE (pure defaults) but never fail SILENT: defaults can differ from the
        # operator's settings in ways that read as "working but idle" on the card, so a
        # rejected row has to be loud enough to find without a database session.
        log.warning("%s.config.invalid — falling back to schema defaults: %s",
                    log_key or automation_id, exc)
        config = validate_config(schema, {}, retired=retired)
    if not enabled:
        return "off", config
    mode = retired_modes_of(automation).get(mode, mode) if mode is not None else None
    if mode is not None and mode in modes:
        return mode, config
    return default_mode or "off", config


async def read_effective_config(automation_id: str) -> dict:
    """The validated config for a REGISTERED automation, schema resolved for you.

    ``read_automation_config`` above is for a control loop that already holds its
    own mode vocabulary. The spawn helper does not — it knows only an
    ``automation_id`` — so it needs the registry's answer for that too.

    Same invariant as its sibling: any failure resolves to pure defaults, which
    for the engine fields means ``claude_code``. An unregistered id (a caller
    passing an ad-hoc origin) answers ``{}`` rather than raising into a spawn.
    """
    from .registry import registry

    try:
        automation = registry.get(automation_id)
    except KeyError:
        return {}
    _mode, config = await read_automation_config(automation, modes=automation.modes)
    return config


async def reconcile_enabled_repos(*, gh_run=None) -> int:
    """Retire every rename alias stored in an ``enabled_repos``. Returns rows changed.

    Canonicalizing on the write path stops a NEW alias from being persisted, but
    the rows that predate a rename are the ones doing damage: GitHub forwards a
    renamed repository forever, so a stale slug keeps resolving and simply becomes
    a SECOND string key for one repo. pr-doctor keys its jobs on ``"owner/repo#N"``
    — two keys meant two concurrent sessions on the same pull request, on the same
    branch, neither aware of the other.

    One query for the union of every row's slugs, not one per row. Fails OPEN and
    idempotent: an unreachable GitHub resolves nothing, so nothing is rewritten and
    the next startup tries again. Runs at startup; the caller owns the scheduling.
    """
    from sqlalchemy import select as sa_select

    from server.db import db_session
    from server.db.models import Automation as AutomationRow
    from server.github.repo_canon import apply_canonical, canonical_slugs

    async with db_session() as db:
        rows = list((await db.execute(sa_select(AutomationRow))).scalars())
        stored = {r.id: list((r.config or {}).get("enabled_repos") or [])
                  for r in rows
                  if isinstance((r.config or {}).get("enabled_repos"), list)}
        union = sorted({slug for repos in stored.values() for slug in repos})
        if not union:
            return 0
        mapping = await asyncio.to_thread(canonical_slugs, union, gh_run=gh_run)
        if not mapping:  # lookup failed entirely — change nothing, retry next start
            return 0

        changed = 0
        for row in rows:
            before = stored.get(row.id)
            if before is None:
                continue
            after = apply_canonical(before, mapping)
            if after != before:
                # Reassign the whole dict: a mutated-in-place JSON column is not
                # seen as dirty by SQLAlchemy and would never be flushed.
                row.config = {**(row.config or {}), "enabled_repos": after}
                changed += 1
                log.info("automations.enabled_repos.reconciled id=%s %s -> %s",
                         row.id, before, after)
        return changed
