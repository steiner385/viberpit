"""Brand-prefixed automation keys that live in DATABASE ROWS, and how to converge them.

An automation's dedupe key is ``<product-slug>:<kind>`` (``spawn_session(dedupe_key=…)``
in ``server/automations/context.py``), and it is persisted — as ``sessions.origin_ref``,
as the ``automation_target_state`` primary-key half that carries the retry counters, and
in the activity feed. So rows written while a product still carried its pre-rebrand
slug outlive any emit-side flip: the emitter composes the key from the product's
CURRENT slug, and the moment that slug changed, every stored key stopped matching the
one being looked up.

There is deliberately **no dual-accept here**, unlike the markers and the MCP tool
prefix. Accepting both spellings of a dedupe key would not be compat, it would be a
second identity for the same work item: ``_existing_session`` would find nothing under
the new key, spawn a duplicate session for work already running, and the loop guard
keyed on the old string would not stop it. One key, converged in place — which is why
this is a migration and not a matcher (design §3, Phase 2c; issue #1369).

Prefix-only rewrite, not the blanket ``replace(col, <legacy>, <new>)`` the spec sketched:
``replace`` rewrites every occurrence anywhere in the string, so a key that merely
*contains* the legacy prefix (``pr-doctor:<legacy-slug>:7``) would be mangled in its
middle. Only a LEADING product slug is ours to rename.

The statements below interpolate rather than bind, and every interpolated value is a
module constant: the table and column names come from :data:`KEY_COLUMNS` and the two
prefixes from ``brand``. Nothing here is reachable from a request, a row, or an
argument — a column name cannot be a bound parameter anyway.
"""
from __future__ import annotations

from dataclasses import dataclass

from viberpit import brand

#: What a key written before the rebrand starts with. Only ever matched, never emitted.
LEGACY_KEY_PREFIX = f"{brand.LEGACY_SLUG}:"
#: What the emitter produces today (via the product slug), and what rows converge to.
KEY_PREFIX = f"{brand.SLUG}:"


@dataclass(frozen=True)
class KeyColumn:
    """One column holding an automation key, and what makes it collide.

    ``unique_with`` names the OTHER columns that, together with this one, must stay
    unique. Empty means the column is free-form and a plain UPDATE is safe; non-empty
    means a legacy row can collide with an already-converged row, and the legacy one is
    the stale copy (nothing composes that key any more), so it is deleted rather than
    updated onto a live row's primary key.
    """

    table: str
    column: str
    unique_with: tuple[str, ...] = ()
    what: str = ""


#: Every column in the schema that stores an automation key. Ordered so the rows that
#: decide behaviour move before the append-only feed that merely describes it.
KEY_COLUMNS: tuple[KeyColumn, ...] = (
    KeyColumn("sessions", "origin_ref", (),
              "the dedupe key `_existing_session` looks a running session up by"),
    KeyColumn("automation_target_state", "target", ("automation_id",),
              "retry counters, the loop guard and the gave-up flag for that key"),
    KeyColumn("initiatives", "origin_ref", (),
              "an initiative's pointer back to what spawned it"),
    KeyColumn("automation_event", "target", (),
              "the automation activity feed (display-only)"),
)


def rewrite_key(value: str | None) -> str | None:
    """*value* with a leading legacy product slug converged, else *value* unchanged.

    Pure, and the definition the SQL below implements — a test can hold the two
    against each other without a database.
    """
    if isinstance(value, str) and value.startswith(LEGACY_KEY_PREFIX):
        return KEY_PREFIX + value[len(LEGACY_KEY_PREFIX):]
    return value


def _new_target_sql(col: KeyColumn) -> str:
    """SQL expression for the converged value of *col* (1-based ``substr``)."""
    return f"'{KEY_PREFIX}' || substr({col.column}, {len(LEGACY_KEY_PREFIX) + 1})"


def select_legacy_sql(col: KeyColumn) -> str:
    """Count of rows in *col* still carrying the legacy prefix — the dry-run readout."""
    return (f"SELECT count(*) FROM {col.table} "
            f"WHERE {col.column} LIKE '{LEGACY_KEY_PREFIX}%'")


def delete_collisions_sql(col: KeyColumn) -> str | None:
    """Drop legacy rows whose converged key is already taken, or None if it cannot happen.

    Only for a ``unique_with`` column: updating such a row would violate the constraint
    and abort the whole migration. The surviving row is the converged one — it is the
    key the emitter composes now, so it is the one the automation will look up next tick.
    """
    if not col.unique_with:
        return None
    # The delete target is NOT aliased: SQLite rejects `DELETE FROM t AS x`, and
    # referring to the outer row by the bare table name is accepted by both dialects.
    same = " AND ".join(f"cur.{c} = {col.table}.{c}" for c in col.unique_with)
    return (f"DELETE FROM {col.table} "
            f"WHERE {col.table}.{col.column} LIKE '{LEGACY_KEY_PREFIX}%' "
            f"AND EXISTS (SELECT 1 FROM {col.table} AS cur WHERE {same} "
            f"AND cur.{col.column} = '{KEY_PREFIX}' || "
            f"substr({col.table}.{col.column}, {len(LEGACY_KEY_PREFIX) + 1}))")


def update_sql(col: KeyColumn) -> str:
    """Converge every remaining legacy-prefixed row in *col*.

    Idempotent by construction: it selects on the legacy prefix and writes the new one,
    so the second run matches nothing. That is also why the collision delete has to run
    first — this statement is what would raise on a duplicate key.
    """
    return (f"UPDATE {col.table} SET {col.column} = {_new_target_sql(col)} "
            f"WHERE {col.column} LIKE '{LEGACY_KEY_PREFIX}%'")
