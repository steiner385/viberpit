"""Guard: every model column must survive the SQLite reconcile path.

The live deploy runs SQLite + _reconcile_sqlite_columns (no Alembic). Reconcile
can ALTER-ADD a NOT NULL column to an existing, populated table ONLY when it can
synthesize a DEFAULT (see _ddl_default_literal). A NOT NULL column it can't
synthesize is skipped at runtime → "no such column" against an old DB (the
2026-06-21 incident). This test fails the PR if such a column is introduced.
"""
from __future__ import annotations

from server.db import _ddl_default_literal
from server.db.models import Base

# Columns intentionally handled by a real migration rather than reconcile.
# Format: "<table>.<column>". Keep empty; add only with a migration + a comment.
#
# The entries below are ORIGINAL schema columns (present since the first CREATE TABLE).
# They have no server-synthesizable default because a valid value is mandatory at INSERT
# time (e.g. a session must have a name+cwd, an app_session must expire, an audit event
# must have a type+identity). They will never need to be ALTER-added to an existing DB,
# so they don't need a reconcile-compatible default. Adding any NEW column here requires
# a real Alembic migration.
RECONCILE_EXEMPT: set[str] = {
    "sessions.name",           # required at session create; no sane default
    "sessions.account_name",   # required at session create; no sane default
    "sessions.cwd",            # required at session create; no sane default
    "app_sessions.identity",   # required at login; no sane default
    "app_sessions.expires_at", # required at login; computed from TTL, no sane default
    "audit_events.type",       # required at audit event write; no sane default
    "audit_events.identity",   # required at audit event write; no sane default
    # attachments: a NEW table (create_all builds it fully-formed on dev SQLite;
    # Alembic 0001_create_attachments builds it on prod). These NOT NULL columns are
    # part of the initial CREATE TABLE and are never ALTER-added to a populated table,
    # and each is mandatory at INSERT (set from the upload), so no synthesizable default.
    "attachments.media_type",  # required at upload; sniffed type
    "attachments.filename",    # required at upload; original filename
    "attachments.byte_size",   # required at upload; computed
    "attachments.sha256",      # required at upload; content hash
    "attachments.uploaded_by", # required at upload; uploader identity
}


def _is_reconcilable(column) -> bool:
    return (
        column.nullable
        or column.primary_key                       # part of CREATE TABLE, never ALTER-added
        or _ddl_default_literal(column) is not None
    )


def test_every_model_column_is_reconcilable():
    offenders = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            key = f"{table.name}.{column.name}"
            if key in RECONCILE_EXEMPT:
                continue
            if not _is_reconcilable(column):
                offenders.append(key)
    assert not offenders, (
        "NOT NULL column(s) the SQLite reconcile path cannot auto-add to an "
        f"existing DB: {offenders}. Give each a scalar/list/dict/datetime default "
        "reconcile can synthesize, or add it via a real migration and list it in "
        "RECONCILE_EXEMPT (with a comment)."
    )
