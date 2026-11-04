"""Durable per-grant MCP refresh health (MCP auth overhaul phase 3).

One ``mcp_grant_health`` row per shared-store grant key, written from each refresh tick's
``summary["outcomes"]``. :func:`record` upserts the rows and returns the transitions the
tick acts on — heal a recovered grant, or push once per (key, generation) for a dead or
expired one. Transitions are decided from each row as it was *before* this tick's upsert.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from viberpit.mcp_oauth_refresh import MAX_ERROR_CHARS

from server.db import db_session, dialect_insert
from server.db.clock import db_utc_now
from server.db.models import McpGrantHealth

TransitionKind = Literal["healed", "alert_dead", "alert_expired"]
_UNHEALTHY_STATES = frozenset({"dead", "expired"})


@dataclass(frozen=True)
class Transition:
    key: str
    server: str
    kind: TransitionKind
    reason: str | None
    generation: str


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat() + "Z"


def generation(last_ok_at: datetime | None) -> str:
    """The grant generation: its last successful refresh (ISO+Z), or ``never``."""
    return _iso(last_ok_at) or "never"


def _wire(r: McpGrantHealth) -> dict:
    return {"key": r.key, "server": r.server, "state": r.state,
            "lastAttemptAt": _iso(r.last_attempt_at), "lastOkAt": _iso(r.last_ok_at),
            "lastOutcome": r.last_outcome, "lastError": r.last_error,
            "consecutiveFailures": r.consecutive_failures, "alertedFor": r.alerted_for,
            "alertedAt": _iso(r.alerted_at), "updatedAt": _iso(r.updated_at)}


async def load(keys: Iterable[str] | None = None) -> dict[str, dict]:
    """Rows by key (all, or only *keys*), wire-shaped with ISO+Z datetimes."""
    q = select(McpGrantHealth)
    if keys is not None:
        wanted = list(keys)
        if not wanted:
            return {}
        q = q.where(McpGrantHealth.key.in_(wanted))
    async with db_session() as db:
        return {r.key: _wire(r) for r in (await db.execute(q)).scalars()}


def _transition(o: dict, prior: McpGrantHealth | None, expired_keys: frozenset[str],
                error: str | None) -> Transition | None:
    key, server, outcome = o["key"], o.get("label") or o["key"], o["outcome"]
    gen = generation(prior.last_ok_at if prior else None)
    alerted_for = prior.alerted_for if prior else None
    if outcome == "refreshed":
        if prior and ((prior.consecutive_failures or 0) > 0 or prior.state in _UNHEALTHY_STATES):
            return Transition(key, server, "healed", None, gen)
    elif outcome == "dead":
        if alerted_for != f"dead:{gen}":
            return Transition(key, server, "alert_dead", None, gen)
    elif outcome == "transient" and key in expired_keys:
        if alerted_for != f"expired:{gen}":
            return Transition(key, server, "alert_expired", error, gen)
    return None


async def record(outcomes: list[dict], *, expired_keys: frozenset[str]) -> list[Transition]:
    """Upsert one row per outcome and return this tick's transitions (see module doc).

    ``refreshed``: state ``live``, ``last_ok_at`` now, failures reset. ``dead``: state ``dead``.
    ``transient``: failures + 1; state ``expired`` if the key is in *expired_keys*, else kept.
    Every outcome sets ``last_attempt_at``, ``last_outcome`` and ``last_error``."""
    if not outcomes:
        return []
    by_key = {o["key"]: o for o in outcomes}   # duplicate keys: last wins
    out: list[Transition] = []
    async with db_session() as db:
        # DB clock rule: read the expression as a scalar so every column gets a concrete naive-UTC value.
        now = (await db.execute(select(db_utc_now()))).scalar_one()
        prior_rows = {r.key: r for r in (await db.execute(
            select(McpGrantHealth).where(McpGrantHealth.key.in_(list(by_key))))).scalars()}
        for key, o in by_key.items():
            outcome = o["outcome"]
            error = o.get("error")
            error = error[:MAX_ERROR_CHARS] if isinstance(error, str) else None
            t = _transition(o, prior_rows.get(key), expired_keys, error)
            if t is not None:
                out.append(t)
            values: dict = {"server": o.get("label") or key, "last_attempt_at": now,
                            "last_outcome": outcome, "last_error": error, "updated_at": now}
            if outcome == "refreshed":
                values.update(state="live", last_ok_at=now, consecutive_failures=0)
            elif outcome == "dead":
                values.update(state="dead")
            elif outcome == "transient":
                if key in expired_keys:
                    values["state"] = "expired"
            stmt = dialect_insert(McpGrantHealth).values(
                key=key, **values,
                **({"consecutive_failures": 1} if outcome == "transient" else {}))
            set_ = {c: getattr(stmt.excluded, c) for c in values}
            if outcome == "transient":
                # Atomic increment of the stored streak (not the value read above).
                set_["consecutive_failures"] = func.coalesce(McpGrantHealth.consecutive_failures, 0) + 1
            await db.execute(stmt.on_conflict_do_update(index_elements=["key"], set_=set_))
    return out


async def mark_signed_in(keys: Iterable[str]) -> None:
    """Record a successful interactive sign-in for *keys*: state ``live``, ``last_ok_at`` now,
    failures reset. The new ``last_ok_at`` starts a new alert generation, so a grant that
    dies again after the sign-in alerts again even if it never refreshed in between."""
    wanted = list(dict.fromkeys(keys))
    if not wanted:
        return
    async with db_session() as db:
        now = (await db.execute(select(db_utc_now()))).scalar_one()
        values = {"state": "live", "last_ok_at": now, "consecutive_failures": 0,
                  "last_outcome": "signed_in", "last_error": None, "updated_at": now}
        for key in wanted:
            stmt = dialect_insert(McpGrantHealth).values(key=key, **values)
            await db.execute(stmt.on_conflict_do_update(
                index_elements=["key"], set_={c: getattr(stmt.excluded, c) for c in values}))


async def mark_alerted(key: str, alerted_for: str) -> None:
    """Record that the push for *alerted_for* (``f"{outcome}:{generation}"``) was delivered.
    Call only after delivery succeeds, so a failed push is retried next tick."""
    async with db_session() as db:
        now = (await db.execute(select(db_utc_now()))).scalar_one()
        stmt = dialect_insert(McpGrantHealth).values(key=key, alerted_for=alerted_for,
                                                     alerted_at=now, updated_at=now)
        await db.execute(stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={c: getattr(stmt.excluded, c) for c in ("alerted_for", "alerted_at", "updated_at")}))
