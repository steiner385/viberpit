"""Persistence for per-repo MCP scope (spec 2026-10-09): global defaults, repo rules, session overrides.

Repo slugs are case-insensitive on GitHub, so ``repo`` is lower-cased on every write and read."""
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
