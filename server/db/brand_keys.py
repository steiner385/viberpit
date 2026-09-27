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

The same shape recurs once more, with a different separator: the SDLC control-band id
(``<product-slug>-pr-cycle-time`` in ``server/sdlc/bands.yaml``) is the identity of
every triage incident the monitor files, and the same argument applies — a band the
yaml spells one way and the row spells another is one breach with two identities, filed
twice. :data:`BAND_ID_COLUMNS` converges those rows (epic #1365).

The statements below interpolate rather than bind, and every interpolated value is a
module constant: the table and column names come from :data:`KEY_COLUMNS` /
:data:`BAND_ID_COLUMNS` and the prefixes from ``brand``. Nothing here is reachable from
a request, a row, or an argument — a column name cannot be a bound parameter anyway.
"""
from __future__ import annotations

from dataclasses import dataclass

from viberpit import brand

#: What a key written before the rebrand starts with. Only ever matched, never emitted.
LEGACY_KEY_PREFIX = f"{brand.LEGACY_SLUG}:"
#: What the emitter produces today (via the product slug), and what rows converge to.
KEY_PREFIX = f"{brand.SLUG}:"

#: The other persisted shape a product slug takes: a control-band id in
#: ``server/sdlc/bands.yaml`` (``<product-slug>-<metric>``). The monitor copies it
#: into ``triage_items.metric``, composes the row's unique ``key`` from it
#: (``<metric>:<rule>:<side>``, ``Breach.key``) and opens the ``summary`` with it —
#: so a band renamed in the yaml stops matching the incident already filed under
#: it, and the next tick files the same breach a second time as a fresh, open row.
LEGACY_BAND_ID_PREFIX = f"{brand.LEGACY_SLUG}-"
BAND_ID_PREFIX = f"{brand.SLUG}-"


@dataclass(frozen=True)
class KeyColumn:
    """One column holding a brand-prefixed key, and what makes it collide.

    ``unique_with`` names the OTHER columns that, together with this one, must stay
    unique; ``unique`` says the column is unique on its own. Neither means the column
    is free-form and a plain UPDATE is safe; either means a legacy row can collide with
    an already-converged row, and ``keep`` decides which of the pair survives:

    * ``"converged"`` — the legacy row is the stale copy. Nothing composes that key any
      more, and the converged row is the one the automation looks up next tick, so the
      legacy one is deleted rather than updated onto a live row's primary key.
    * ``"legacy"`` — the legacy row is the audit record: it carries the disposition a
      human (or Intake) already gave the incident. The converged twin can only be a
      re-detection filed in the window between the id flip and this converge, so it
      goes and the record takes the new key.

    ``legacy_prefix`` / ``prefix`` are the pair being rewritten; only a LEADING match is
    ever touched.
    """

    table: str
    column: str
    unique_with: tuple[str, ...] = ()
    what: str = ""
    legacy_prefix: str = LEGACY_KEY_PREFIX
    prefix: str = KEY_PREFIX
    unique: bool = False
    keep: str = "converged"

    @property
    def can_collide(self) -> bool:
        return self.unique or bool(self.unique_with)


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

#: Every column that stores a control-band id (revision ``0074_brand_control_band_id``).
#: The unique ``key`` goes first: its collision delete removes the whole twin row, so
#: the two free-form columns after it never see a duplicate.
BAND_ID_COLUMNS: tuple[KeyColumn, ...] = (
    KeyColumn("triage_items", "key", (),
              "the breach identity `upsert_breach` refreshes an open incident by",
              legacy_prefix=LEGACY_BAND_ID_PREFIX, prefix=BAND_ID_PREFIX,
              unique=True, keep="legacy"),
    KeyColumn("triage_items", "metric", (),
              "the band id the incident queue shows and Intake reports",
              legacy_prefix=LEGACY_BAND_ID_PREFIX, prefix=BAND_ID_PREFIX),
    KeyColumn("triage_items", "summary", (),
              "the one-line title, which opens with the band id (display-only)",
              legacy_prefix=LEGACY_BAND_ID_PREFIX, prefix=BAND_ID_PREFIX),
)

#: What the operator one-shot walks, in order.
ALL_KEY_COLUMNS: tuple[KeyColumn, ...] = KEY_COLUMNS + BAND_ID_COLUMNS


def rewrite_key(value: str | None, legacy_prefix: str = LEGACY_KEY_PREFIX,
                prefix: str = KEY_PREFIX) -> str | None:
    """*value* with a leading legacy product slug converged, else *value* unchanged.

    Pure, and the definition the SQL below implements — a test can hold the two
    against each other without a database.
    """
    if isinstance(value, str) and value.startswith(legacy_prefix):
        return prefix + value[len(legacy_prefix):]
    return value


def _converged_sql(col: KeyColumn, qualified: str) -> str:
    """SQL expression for the converged value of *qualified* (1-based ``substr``)."""
    return f"'{col.prefix}' || substr({qualified}, {len(col.legacy_prefix) + 1})"


def select_legacy_sql(col: KeyColumn) -> str:
    """Count of rows in *col* still carrying the legacy prefix — the dry-run readout."""
    return (f"SELECT count(*) FROM {col.table} "
            f"WHERE {col.column} LIKE '{col.legacy_prefix}%'")


def delete_collisions_sql(col: KeyColumn) -> str | None:
    """Drop the losing half of every legacy/converged pair, or None if none can exist.

    Only for a column that takes part in a uniqueness constraint: updating a legacy row
    onto a key already held would violate it and abort the whole migration. Which half
    loses is ``col.keep`` — see :class:`KeyColumn`.
    """
    if not col.can_collide:
        return None
    # The delete target is NOT aliased: SQLite rejects `DELETE FROM t AS x`, and
    # referring to the outer row by the bare table name is accepted by both dialects.
    same = "".join(f" AND cur.{c} = {col.table}.{c}" for c in col.unique_with)
    mine, other = f"{col.table}.{col.column}", f"cur.{col.column}"
    if col.keep == "converged":
        # Delete the legacy row whose converged key some other row already holds.
        return (f"DELETE FROM {col.table} "
                f"WHERE {mine} LIKE '{col.legacy_prefix}%' "
                f"AND EXISTS (SELECT 1 FROM {col.table} AS cur WHERE {other} = "
                f"{_converged_sql(col, mine)}{same})")
    # Delete the converged row that a still-legacy row is about to converge onto.
    return (f"DELETE FROM {col.table} "
            f"WHERE {mine} LIKE '{col.prefix}%' "
            f"AND EXISTS (SELECT 1 FROM {col.table} AS cur "
            f"WHERE {other} LIKE '{col.legacy_prefix}%' "
            f"AND {_converged_sql(col, other)} = {mine}{same})")


def update_sql(col: KeyColumn) -> str:
    """Converge every remaining legacy-prefixed row in *col*.

    Idempotent by construction: it selects on the legacy prefix and writes the new one,
    so the second run matches nothing. That is also why the collision delete has to run
    first — this statement is what would raise on a duplicate key.
    """
    return (f"UPDATE {col.table} SET {col.column} = {_converged_sql(col, col.column)} "
            f"WHERE {col.column} LIKE '{col.legacy_prefix}%'")
