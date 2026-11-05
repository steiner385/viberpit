"""The owner's curated marketplaces (D-16c, D1): ``app_settings.curated_marketplaces``.

A JSON object ``{name: normalized_source}`` — one record per marketplace the owner
CONFIRMED through ViberPit (``POST /library/marketplaces`` or ``…/{name}/confirm``), bound
to ``plugin_policy.normalize_source`` of the source they were shown. Only these, and only
while the configured source still normalizes equal (``plugin_policy.check_curated``), are
installable; reviewed Anthropic Directory plugins need no record.

Every other configured marketplace is UNCONFIRMED — including those configured by hand
before D-16c shipped (fix round 1 superseded the "ship-time counts as curated" ruling:
``known_marketplaces.json`` is agent-writable). :func:`curation_state` names the state the
UI shows: ``confirmed``, ``unconfirmed`` (offer Confirm) or ``source_changed`` (re-confirm).
Nothing is seeded: the record starts empty.
"""
from __future__ import annotations

import json
import logging

from server.library import plugin_policy

log = logging.getLogger(__name__)

CONFIRMED = "confirmed"
UNCONFIRMED = "unconfirmed"
SOURCE_CHANGED = "source_changed"


def _decode(raw: str | None) -> dict[str, str]:
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("curated_marketplaces unreadable; treating every marketplace as unconfirmed")
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if plugin_policy.valid_marketplace_name(k) and isinstance(v, str) and v}


async def _row(db):  # noqa: ANN202 — AppSettings
    from server.api.settings import _get_or_create
    return await _get_or_create(db)


async def confirmed(db) -> dict[str, str]:
    """``{name: normalized_source}`` of every confirmed marketplace."""
    return _decode((await _row(db)).curated_marketplaces)


async def confirm(db, name: str, normalized_source: str) -> None:
    """Record that the owner confirmed *name* at *normalized_source* (replacing any
    earlier source: a re-confirm after ``source_changed``)."""
    row = await _row(db)
    current = _decode(row.curated_marketplaces)
    current[name] = normalized_source
    row.curated_marketplaces = json.dumps(dict(sorted(current.items())))
    await db.commit()


async def remove(db, name: str) -> None:
    row = await _row(db)
    current = _decode(row.curated_marketplaces)
    if current.pop(name, None) is not None:
        row.curated_marketplaces = json.dumps(dict(sorted(current.items())))
        await db.commit()


def curation_state(name: str, records: dict[str, str], configured_source: str | None) -> str:
    """The per-marketplace state the UI shows (and ``check_curated`` enforces)."""
    recorded = records.get(name)
    if recorded is None:
        return UNCONFIRMED
    return CONFIRMED if configured_source == recorded else SOURCE_CHANGED
