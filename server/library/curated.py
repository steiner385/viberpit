"""The owner's curated marketplace set (D-16c, D1): ``app_settings.curated_marketplaces``.

Only plugins from these marketplaces (and reviewed Anthropic Directory plugins) may be
installed through ViberPit. It is a ViberPit record, not ``known_marketplaces.json``, so a
marketplace an agent adds by running ``claude plugin marketplace add`` in a session never
becomes installable from the UI.

Ruling (progress.md): every marketplace already configured on the host when D-16c ships
counts as curated — the owner added those by hand. The column is NULL until first read,
which seeds it once from the edge host's ``known_marketplaces.json``; from then on only
``POST /library/marketplaces`` adds and ``DELETE`` removes.
"""
from __future__ import annotations

import asyncio
import json
import logging

from server.library import scan

log = logging.getLogger(__name__)


def _decode(raw: str | None) -> set[str] | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("curated_marketplaces unreadable; treating as empty")
        return set()
    return {n for n in data if isinstance(n, str)} if isinstance(data, list) else set()


async def _row(db):  # noqa: ANN202 — AppSettings
    from server.api.settings import _get_or_create
    return await _get_or_create(db)


async def curated(db) -> frozenset[str]:
    """The curated set, seeding it on first use (see module doc)."""
    row = await _row(db)
    names = _decode(row.curated_marketplaces)
    if names is None:
        names = set(await asyncio.to_thread(scan.known_marketplaces))
        row.curated_marketplaces = json.dumps(sorted(names))
        await db.commit()
        log.info("curated_marketplaces seeded from known_marketplaces.json count=%d", len(names))
    return frozenset(names)


async def add(db, name: str) -> None:
    current = set(await curated(db))
    current.add(name)
    (await _row(db)).curated_marketplaces = json.dumps(sorted(current))
    await db.commit()


async def remove(db, name: str) -> None:
    current = set(await curated(db))
    current.discard(name)
    (await _row(db)).curated_marketplaces = json.dumps(sorted(current))
    await db.commit()
