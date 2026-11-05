"""Persistence for per-repo MCP scope (spec 2026-10-09): global defaults, repo rules, session overrides.

Repo slugs are case-insensitive on GitHub, so ``repo`` is lower-cased on every write and read.

D-16b adds a tool dimension on the same rows: ``mcp_server_scope.tool_defaults`` (the
Settings default per tool) and ``sessions.mcp_tool_overrides`` (a session's live override).
There is no per-repo tool level."""
from __future__ import annotations

from sqlalchemy import delete, select, update

from server.db import db_session, dialect_insert
from server.db.clock import db_utc_now
from server.db.models import McpRepoRule, McpServerScope
from server.db.models import Session as DbSession


async def server_defaults() -> dict[str, bool]:
    async with db_session() as db:
        rows = (await db.execute(select(McpServerScope))).scalars()
        return {r.server: r.default_enabled for r in rows}


async def repo_rules(repo: str) -> dict[str, bool]:
    async with db_session() as db:
        q = select(McpRepoRule).where(McpRepoRule.repo == repo.lower())
        return {r.server: r.enabled for r in (await db.execute(q)).scalars()}


async def all_rules() -> list[dict]:
    async with db_session() as db:
        q = select(McpRepoRule).order_by(McpRepoRule.repo, McpRepoRule.server)
        return [{"repo": r.repo, "server": r.server, "enabled": r.enabled}
                for r in (await db.execute(q)).scalars()]


async def set_default(server: str, enabled: bool) -> None:
    async with db_session() as db:
        now = (await db.execute(select(db_utc_now()))).scalar_one()
        stmt = dialect_insert(McpServerScope).values(server=server, default_enabled=enabled, updated_at=now)
        stmt = stmt.on_conflict_do_update(
            index_elements=["server"],
            set_={"default_enabled": stmt.excluded.default_enabled, "updated_at": stmt.excluded.updated_at})
        await db.execute(stmt)


async def set_repo_rule(repo: str, server: str, enabled: bool) -> None:
    async with db_session() as db:
        now = (await db.execute(select(db_utc_now()))).scalar_one()
        stmt = dialect_insert(McpRepoRule).values(
            repo=repo.lower(), server=server, enabled=enabled, updated_at=now)
        stmt = stmt.on_conflict_do_update(
            index_elements=["repo", "server"],
            set_={"enabled": stmt.excluded.enabled, "updated_at": stmt.excluded.updated_at})
        await db.execute(stmt)


async def delete_repo_rule(repo: str, server: str) -> bool:
    async with db_session() as db:
        res = await db.execute(delete(McpRepoRule).where(
            McpRepoRule.repo == repo.lower(), McpRepoRule.server == server))
        return bool(res.rowcount)  # type: ignore[attr-defined]


async def delete_server(server: str) -> None:
    """Drop a server's global default and every repo rule for it."""
    async with db_session() as db:
        await db.execute(delete(McpRepoRule).where(McpRepoRule.server == server))
        await db.execute(delete(McpServerScope).where(McpServerScope.server == server))


async def tool_defaults() -> dict[str, dict[str, bool]]:
    """Server key -> {tool: enabled} for every server with a Settings tool default."""
    async with db_session() as db:
        rows = (await db.execute(select(McpServerScope))).scalars()
        return {r.server: _bools(r.tool_defaults) for r in rows if r.tool_defaults}


async def set_tool_default(server: str, tool: str, enabled: bool) -> dict[str, bool]:
    """Set the Settings default of *server*'s *tool*; returns the server's tool defaults.

    A server with no scope row gets one with ``default_enabled=True`` — the same as no
    row for the server-level resolution."""
    async with db_session() as db:
        now = (await db.execute(select(db_utc_now()))).scalar_one()
        await db.execute(dialect_insert(McpServerScope).values(
            server=server, default_enabled=True, updated_at=now,
        ).on_conflict_do_nothing(index_elements=["server"]))
        # Lock the row before reading it, as set_session_override does: a concurrent
        # write of another tool waits and re-reads instead of clobbering this one.
        await db.execute(update(McpServerScope).where(McpServerScope.server == server)
                         .values(server=McpServerScope.server))
        row = (await db.execute(select(McpServerScope).where(McpServerScope.server == server)
                                .execution_options(populate_existing=True))).scalar_one()
        merged = {**_bools(row.tool_defaults), tool: enabled}
        row.tool_defaults = merged      # NEW dict: in-place JSON mutation isn't detected
        row.updated_at = now
        return dict(merged)


def _bools(d: object) -> dict[str, bool]:
    return {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, bool)} \
        if isinstance(d, dict) else {}


def _nested_bools(d: object) -> dict[str, dict[str, bool]]:
    return {k: b for k, v in d.items() if isinstance(k, str) and (b := _bools(v))} \
        if isinstance(d, dict) else {}


async def session_tool_overrides(session_id: str) -> dict[str, dict[str, bool]]:
    async with db_session() as db:
        row = await db.get(DbSession, session_id)
        return _nested_bools(row.mcp_tool_overrides) if row else {}


async def set_session_tool_override(session_id: str, server: str, tool: str,
                                    enabled: bool | None) -> bool | None:
    """Record (or with None clear) a session's override of *server*'s *tool*; returns the
    override it replaced (None: there was none), so a caller can restore it.
    ``KeyError`` for a gone row."""
    async with db_session() as db:
        locked = await db.execute(
            update(DbSession).where(DbSession.id == session_id).values(id=DbSession.id))
        if not locked.rowcount:  # type: ignore[attr-defined]
            raise KeyError(session_id)
        row = (await db.execute(select(DbSession).where(DbSession.id == session_id)
                                .execution_options(populate_existing=True))).scalar_one()
        merged = _nested_bools(row.mcp_tool_overrides)
        tools = dict(merged.get(server, {}))
        previous = tools.pop(tool, None)
        if enabled is not None:
            tools[tool] = enabled
        if tools:
            merged[server] = tools
        else:
            merged.pop(server, None)
        row.mcp_tool_overrides = merged or None
        return previous


async def session_overrides(session_id: str) -> dict[str, bool]:
    async with db_session() as db:
        row = await db.get(DbSession, session_id)
        return dict(row.mcp_overrides or {}) if row else {}


async def set_session_override(session_id: str, server: str, enabled: bool) -> dict[str, bool]:
    async with db_session() as db:
        # Take the row's write lock BEFORE reading: a concurrent toggle of another server must wait
        # and re-read, not clobber. A no-op UPDATE locks on Postgres (row lock) and on SQLite (it
        # upgrades the transaction to a writer up front; FOR UPDATE is ignored there).
        locked = await db.execute(
            update(DbSession).where(DbSession.id == session_id).values(id=DbSession.id))
        if not locked.rowcount:  # type: ignore[attr-defined]
            raise KeyError(session_id)
        row = (await db.execute(select(DbSession).where(DbSession.id == session_id)
                                .execution_options(populate_existing=True))).scalar_one()
        merged = {**(row.mcp_overrides or {}), server: enabled}
        row.mcp_overrides = merged  # NEW dict: in-place mutation of a JSON column isn't detected
        return dict(merged)
