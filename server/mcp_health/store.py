"""Persistence for MCP live status (spec 2026-10-09). Rows are the latest observation per key."""
from __future__ import annotations

from sqlalchemy import select

from server.db import db_session
from server.db.clock import db_utc_now
from server.db.models import McpServerHealth
from server.mcp_health.status import McpServerState


def short_name(server_key: str) -> str:
    """Strip the ``plugin:<p>:`` prefix: ``plugin:railway:railway`` -> ``railway``."""
    return server_key.rsplit(":", 1)[-1] if server_key.startswith("plugin:") else server_key


async def upsert_states(account: str, host: str, states: list[McpServerState], *,
                        source: str, session_id: str | None) -> None:
    """Overwrite the (account, host, server) row for each state. Select-then-write in one
    transaction so it is dialect-neutral."""
    if not states:
        return
    async with db_session() as db:
        # DB clock rule: read the expression as a scalar so observed_at is a concrete naive-UTC datetime.
        now = (await db.execute(select(db_utc_now()))).scalar_one()
        existing = {r.server: r for r in (await db.execute(select(McpServerHealth).where(
            McpServerHealth.account == account, McpServerHealth.host == host,
            McpServerHealth.server.in_([s.name for s in states])))).scalars()}
        for s in states:
            row = existing.get(s.name) or McpServerHealth(account=account, host=host, server=s.name)
            row.status, row.error, row.tool_count, row.scope = s.status, s.error, s.tool_count, s.scope
            row.source, row.session_id, row.observed_at = source, session_id, now
            db.add(row)


def _wire(r: McpServerHealth) -> dict:
    return {"account": r.account, "host": r.host, "server": r.server, "status": r.status,
            "error": r.error, "toolCount": r.tool_count, "scope": r.scope, "source": r.source,
            "sessionId": r.session_id, "observedAt": r.observed_at.isoformat() + "Z"}


async def rows(server: str | None = None) -> list[dict]:
    """All rows (optionally those whose short name matches ``server``), wire-shaped."""
    async with db_session() as db:
        q = select(McpServerHealth).order_by(McpServerHealth.server, McpServerHealth.account, McpServerHealth.host)
        out = [_wire(r) for r in (await db.execute(q)).scalars()]
    return [r for r in out if server is None or short_name(r["server"]) == short_name(server)]


async def needs_auth_by_server() -> dict[str, list[str]]:
    """Short server name -> sorted accounts whose latest row is ``needs-auth``."""
    acc: dict[str, set[str]] = {}
    for r in await rows():
        if r["status"] == "needs-auth":
            acc.setdefault(short_name(r["server"]), set()).add(r["account"])
    return {k: sorted(v) for k, v in acc.items()}


async def login_key_for(short: str) -> str:
    """The full server key recorded for ``short`` (e.g. ``plugin:p:name``), else ``short`` itself."""
    for r in await rows(short):
        return r["server"]
    return short
