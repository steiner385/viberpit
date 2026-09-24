"""Refuse a SQLite file on a shared mount — the open-time half of #1005/#1010.

The shape (#2016): a default path for runtime state that assumes ONE local writer
resolves to ``~/.claude-accounts``, which on every session runner is an sshfs mount
of the authority's directory. Two instances were fixed by moving the default
(``CODEX_HOME``, #1005; the JSON log dir, #1010). The remaining two are SQLite
files — the dev app DB (``server/config.py``) and ``pipeline_history.db``
(``server/pipeline/history_migrate.py:default_dest``) — that are local today only
because the edge happens to run on dobby, where the store is not a mount.

Their defaults stay where they are (moving them would orphan existing data);
instead each is checked when it is opened, so "never on a share" is a property of
the code rather than of where the edge runs.

Policy differs from the Codex refusal on one point: an *undeterminable* mount
table is allowed, not refused. Refusing the app DB refuses the whole process, and
"we could not read ``/proc/mounts``" is a sandbox/non-Linux dev shape, not the
fleet's. A *known* network filesystem is always refused.
"""
from __future__ import annotations

from pathlib import Path

from server.fsmounts import Mounts, filesystem_type_for, is_network_fstype


class SharedSqliteRefused(RuntimeError):
    """A SQLite file would be opened on a network filesystem."""


def assert_local_sqlite(path: str | Path, *, mounts: Mounts | None = None) -> None:
    """Raise :class:`SharedSqliteRefused` if *path* is on a network filesystem.

    SQLite's WAL and locking assume a local filesystem; over sshfs they silently
    corrupt (the 2026-09-04 torn-read incident, #1005). ``mounts`` overrides the
    live mount table, the same injection seam ``assert_local_filesystem`` takes.
    """
    fstype = filesystem_type_for(path, mounts=mounts)
    if fstype is not None and is_network_fstype(fstype):
        raise SharedSqliteRefused(
            f"refusing SQLite database at {path}: it is on a network filesystem "
            f"({fstype}); SQLite locking and WAL are unsafe there. Point the path "
            "at a host-local directory (or use Postgres for the app DB)."
        )


def sqlite_file_from_url(db_url: str) -> str | None:
    """The on-disk file a SQLAlchemy SQLite URL opens, or ``None`` if none.

    ``None`` for non-SQLite URLs and for in-memory databases, which have no file
    to put on a share.
    """
    from sqlalchemy.engine import make_url

    url = make_url(db_url)
    if not url.get_backend_name() == "sqlite":
        return None
    database = url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        return None
    return database
