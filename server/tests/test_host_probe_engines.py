"""The probe records what each runner advertises — and Codex never touches health.

The isolation rule is the point of this file. A runner with no Codex binary, a
drifted app-server contract, or external auth switched off is still a perfectly
good Claude runner. If any of that leaked into ``healthy`` it would take Claude
dispatch down with it AND change deploy targeting, because
``server.deploy.runners.is_deployable`` reads ``last_error``.
"""
from __future__ import annotations

import asyncio

from server.db.models import Host

_CODEX_DOWN = {
    "claude_code": {"available": True},
    "codex": {"available": False, "reason": "binary_not_found"},
}
_CODEX_UP = {
    "claude_code": {"available": True},
    "codex": {"available": True, "cliVersion": "codex-cli 0.153.2",
              "contractSha256": "abc"},
}


def _seed(tmp_path, dbname, *hosts):
    from server.db import db_session, init_db

    async def go():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / dbname}")
        async with db_session() as db:
            for h in hosts:
                db.add(h)
    return go


def _row(name: str):
    from sqlalchemy import select as sa_select

    from server.db import db_session

    async def go():
        async with db_session() as db:
            return (await db.execute(
                sa_select(Host).where(Host.name == name))).scalars().first()
    return go


def test_probe_persists_the_engine_advertisement(tmp_path):
    from server.sessions.host_probe import probe_all

    async def fetch(endpoint):
        return {"sha": "abc1234", "protocol": 1, "engines": _CODEX_UP}

    async def run():
        await _seed(tmp_path, "pe1.db", Host(name="rocky", endpoint="https://rocky:9443"))()
        await probe_all(fetch=fetch)
        return await _row("rocky")()

    row = asyncio.run(run())
    assert row.engines == _CODEX_UP
    assert row.healthy is True


def test_an_unavailable_codex_leaves_the_host_healthy(tmp_path):
    """The isolation rule, stated as a test."""
    from server.sessions.host_probe import probe_all

    async def fetch(endpoint):
        return {"sha": "abc1234", "protocol": 1, "engines": _CODEX_DOWN}

    async def run():
        await _seed(tmp_path, "pe2.db", Host(name="rocky", endpoint="https://rocky:9443"))()
        await probe_all(fetch=fetch)
        return await _row("rocky")()

    row = asyncio.run(run())
    assert row.healthy is True
    assert row.last_error is None
    assert row.engines["codex"]["available"] is False


def test_a_legacy_daemon_records_no_advertisement_rather_than_a_synthetic_one(tmp_path):
    """NULL must stay NULL: it is how an un-upgraded runner is told apart.

    Writing the Claude-only default here would make a legacy daemon and an upgraded
    Claude-only daemon indistinguishable, and the compatibility default could never
    be removed in the final cleanup PR because nothing would still be observable.
    """
    from server.sessions.host_probe import probe_all

    async def fetch(endpoint):
        return {"sha": "abc1234", "protocol": 1}

    async def run():
        await _seed(tmp_path, "pe3.db", Host(name="rocky", endpoint="https://rocky:9443"))()
        await probe_all(fetch=fetch)
        return await _row("rocky")()

    assert asyncio.run(run()).engines is None


def test_a_fenced_host_still_records_what_it_can_run(tmp_path):
    """Skew/protocol fencing is about WHETHER to dispatch, not about capability.

    Blanking the advertisement on a fenced host would make the fleet page forget
    what a runner offers for exactly as long as it is out of date — which is
    precisely when an operator is looking at it.
    """
    from server.sessions.host_probe import probe_all

    async def fetch(endpoint):
        return {"sha": "abc1234", "protocol": 999, "engines": _CODEX_UP}

    async def run():
        await _seed(tmp_path, "pe4.db", Host(name="rocky", endpoint="https://rocky:9443"))()
        await probe_all(fetch=fetch)
        return await _row("rocky")()

    row = asyncio.run(run())
    assert row.healthy is False
    assert row.last_error.startswith("protocol mismatch")
    assert row.engines == _CODEX_UP


def test_an_unreachable_host_keeps_its_last_known_advertisement(tmp_path):
    """Unreachable means unknown, not "offers nothing" — health already says down."""
    from server.sessions.host_probe import probe_all

    async def fetch(endpoint):
        raise OSError("connection refused")

    async def run():
        await _seed(tmp_path, "pe5.db", Host(
            name="rocky", endpoint="https://rocky:9443", engines=_CODEX_UP))()
        await probe_all(fetch=fetch)
        return await _row("rocky")()

    row = asyncio.run(run())
    assert row.healthy is False
    assert row.engines == _CODEX_UP


def test_a_non_dict_advertisement_is_ignored_not_stored(tmp_path):
    from server.sessions.host_probe import probe_all

    async def fetch(endpoint):
        return {"sha": "abc1234", "protocol": 1, "engines": ["codex"]}

    async def run():
        await _seed(tmp_path, "pe6.db", Host(name="rocky", endpoint="https://rocky:9443"))()
        await probe_all(fetch=fetch)
        return await _row("rocky")()

    row = asyncio.run(run())
    assert row.engines is None
    assert row.healthy is True
