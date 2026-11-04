"""Durable per-grant MCP refresh health (MCP auth overhaul phase 3).

One ``mcp_grant_health`` row per shared-store grant key, written from each refresh tick's
``summary["outcomes"]``. :func:`record` upserts the rows and returns the grants that healed
(decided from each row as it was *before* this tick's upsert).

Alerts are decided from durable state only, never from one tick's outcomes: the refresher
reports a ``dead`` outcome exactly once (it clears the bundle, so the key leaves the due
set), and a failure between that swap and :func:`mark_alerted` must not lose the alert.
Each tick :func:`promote_cleared` marks a signed-in grant whose bundle is cleared ``dead``,
then :func:`pending_alerts` returns every dead or expired row not yet alerted for its
current (state, generation).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func, or_, select, update
from viberpit.mcp_oauth_refresh import MAX_ERROR_CHARS

from server.db import db_session, dialect_insert
from server.db.clock import db_utc_now
from server.db.models import McpGrantHealth

TransitionKind = Literal["healed", "alert_dead", "alert_expired"]
_UNHEALTHY_STATES = frozenset({"dead", "expired"})
# A sign-in this recent is never promoted dead: the cleared read may predate it.
SIGN_IN_GRACE = timedelta(seconds=60)


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


def _healed(o: dict, prior: McpGrantHealth | None) -> Transition | None:
    """A ``refreshed`` grant that was failing or unhealthy before this tick has healed."""
    if o["outcome"] != "refreshed" or prior is None:
        return None
    if (prior.consecutive_failures or 0) > 0 or prior.state in _UNHEALTHY_STATES:
        return Transition(o["key"], o.get("label") or o["key"], "healed", None,
                          generation(prior.last_ok_at))
    return None


async def record(outcomes: list[dict], *, expired_keys: frozenset[str]) -> list[Transition]:
    """Upsert one row per outcome and return this tick's ``healed`` transitions.

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
            t = _healed(o, prior_rows.get(key))
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


async def promote_cleared(keys: Iterable[str]) -> None:
    """Mark ``dead`` each of *keys* (store keys whose bundle is cleared) that was signed in —
    has a ``last_ok_at`` — and is not already dead. Catches a death whose ``dead`` outcome was
    never recorded (it is reported once) and one the CLI cleared itself. A key with no row or
    no ``last_ok_at`` (never signed in here) is left alone, so it never alerts. A key signed
    in within :data:`SIGN_IN_GRACE` is skipped too: the caller's cleared read may predate it."""
    wanted = list(dict.fromkeys(keys))
    if not wanted:
        return
    async with db_session() as db:
        now = (await db.execute(select(db_utc_now()))).scalar_one()
        await db.execute(
            update(McpGrantHealth)
            .where(McpGrantHealth.key.in_(wanted), McpGrantHealth.last_ok_at.is_not(None),
                   or_(McpGrantHealth.state.is_(None), McpGrantHealth.state != "dead"),
                   or_(McpGrantHealth.last_outcome.is_(None),
                       McpGrantHealth.last_outcome != "signed_in",
                       McpGrantHealth.updated_at.is_(None),
                       McpGrantHealth.updated_at < now - SIGN_IN_GRACE))
            .values(state="dead", updated_at=now))


def _alert(r: McpGrantHealth) -> Transition | None:
    gen = generation(r.last_ok_at)
    if r.state not in _UNHEALTHY_STATES or r.alerted_for == f"{r.state}:{gen}":
        return None
    server = r.server or r.key
    if r.state == "dead":
        return Transition(r.key, server, "alert_dead", None, gen)
    return Transition(r.key, server, "alert_expired", r.last_error, gen)


async def pending_alerts() -> list[Transition]:
    """Every dead or expired grant not yet alerted for its current ``f"{state}:{generation}"``
    — the single source of alert decisions. Re-derived from the table each tick, so an alert
    whose push or :func:`mark_alerted` failed is returned again next tick."""
    async with db_session() as db:
        rows = (await db.execute(select(McpGrantHealth).where(
            McpGrantHealth.state.in_(sorted(_UNHEALTHY_STATES))).order_by(McpGrantHealth.key))).scalars()
        return [t for r in rows if (t := _alert(r)) is not None]


async def mark_signed_in(keys: Iterable[str], *, server: str | None = None) -> None:
    """Record a successful interactive sign-in for *keys*: state ``live``, ``last_ok_at`` now,
    failures reset, and *server* (the display name) when given. The new ``last_ok_at``
    starts a new alert generation, so a grant that dies again after the sign-in alerts
    again even if it never refreshed in between."""
    wanted = list(dict.fromkeys(keys))
    if not wanted:
        return
    async with db_session() as db:
        now = (await db.execute(select(db_utc_now()))).scalar_one()
        values = {"state": "live", "last_ok_at": now, "consecutive_failures": 0,
                  "last_outcome": "signed_in", "last_error": None, "updated_at": now,
                  **({"server": server} if server else {})}
        for key in wanted:
            stmt = dialect_insert(McpGrantHealth).values(key=key, **values)
            await db.execute(stmt.on_conflict_do_update(
                index_elements=["key"], set_={c: getattr(stmt.excluded, c) for c in values}))


async def mark_alerted(key: str, alerted_for: str) -> None:
    """Record that the push for *alerted_for* (``f"{state}:{generation}"``) was delivered.
    Call only after delivery succeeds, so a failed push is retried next tick."""
    async with db_session() as db:
        now = (await db.execute(select(db_utc_now()))).scalar_one()
        stmt = dialect_insert(McpGrantHealth).values(key=key, alerted_for=alerted_for,
                                                     alerted_at=now, updated_at=now)
        await db.execute(stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={c: getattr(stmt.excluded, c) for c in ("alerted_for", "alerted_at", "updated_at")}))
