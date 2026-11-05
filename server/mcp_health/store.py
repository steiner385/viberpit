"""Persistence for MCP live status (spec 2026-10-09). Rows are the latest observation per key."""
from __future__ import annotations

from viberpit.mcp_names import short_name
from sqlalchemy import func, null, select

from server.db import db_session, dialect_insert
from server.db.clock import db_utc_now
from server.db.models import McpServerHealth
from server.mcp_health.status import McpServerState, McpTool, tool_from_wire


async def upsert_states(account: str, host: str, states: list[McpServerState], *,
                        source: str, session_id: str | None) -> None:
    """Atomically overwrite the (account, host, server) row for each state (ON CONFLICT DO UPDATE),
    so concurrent writers of one key never raise IntegrityError. Duplicate names: last wins."""
    if not states:
        return
    by_name = {s.name: s for s in states}
    async with db_session() as db:
        # DB clock rule: read the expression as a scalar so observed_at is a concrete naive-UTC datetime.
        now = (await db.execute(select(db_utc_now()))).scalar_one()
        for s in by_name.values():
            # Only a connected server's list is its real tool list (a disabled one lists
            # none); anything else keeps the last one seen (D-16b).
            seen = s.status == "connected" and s.tools is not None
            stmt = dialect_insert(McpServerHealth).values(
                account=account, host=host, server=s.name, status=s.status, error=s.error,
                tool_count=s.tool_count, scope=s.scope, source=source, session_id=session_id,
                observed_at=now,
                tools=[t.to_wire() for t in s.tools] if seen and s.tools is not None else null(),
                tools_seen_at=now if seen else null())
            set_ = {c: getattr(stmt.excluded, c) for c in
                    ("status", "error", "tool_count", "scope", "source", "session_id", "observed_at")}
            for c in ("tools", "tools_seen_at"):
                set_[c] = func.coalesce(getattr(stmt.excluded, c), getattr(McpServerHealth, c))
            stmt = stmt.on_conflict_do_update(index_elements=["account", "host", "server"], set_=set_)
            await db.execute(stmt)


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


async def needs_auth_by_server(*, enabled_accounts: set[str] | None = None) -> dict[str, list[str]]:
    """Short server name -> sorted accounts whose latest row is ``needs-auth``;
    only *enabled_accounts* when given."""
    acc: dict[str, set[str]] = {}
    for r in await rows():
        if r["status"] == "needs-auth" and (enabled_accounts is None or r["account"] in enabled_accounts):
            acc.setdefault(short_name(r["server"]), set()).add(r["account"])
    return {k: sorted(v) for k, v in acc.items()}


async def login_key_for(short: str) -> str:
    """The full server key recorded for ``short`` (e.g. ``plugin:p:name``), else ``short`` itself."""
    for r in await rows(short):
        return r["server"]
    return short


async def last_seen_tools() -> dict[str, tuple[list[McpTool], str]]:
    """Server key -> (its most recently seen tool list, when, ISO+Z) across every
    account and host (D-16b: the Settings tool list, which has no live session)."""
    async with db_session() as db:
        q = (select(McpServerHealth.server, McpServerHealth.tools, McpServerHealth.tools_seen_at)
             .where(McpServerHealth.tools_seen_at.is_not(None))
             .order_by(McpServerHealth.tools_seen_at))
        out: dict[str, tuple[list[McpTool], str]] = {}
        for server, tools, seen_at in (await db.execute(q)).all():
            if isinstance(tools, list):     # later rows (newer) overwrite older ones
                out[server] = ([t for t in map(tool_from_wire, tools) if t is not None],
                               seen_at.isoformat() + "Z")
    return out
