"""The database clock, as a naive-UTC SQL expression on every dialect.

Every persisted timestamp in this schema is naive UTC (``datetime.utcnow()`` /
``_utcnow()`` at the writer). A DB-side clock has to honour that convention or
it cannot be compared with anything else in the schema — and the two dialects
we run on disagree about what ``CURRENT_TIMESTAMP`` means:

* **SQLite** — a naive UTC string. Written or SELECTed, it lines up with the
  rest of the schema unchanged.
* **Postgres** — a ``timestamptz``. SELECTed, asyncpg hands back an *aware*
  datetime (``TypeError`` the moment it meets a naive column value). Written
  into a naive ``timestamp`` column it is cast through the session ``TimeZone``
  (production: America/New_York) and stored as *local wall clock*, hours off the
  naive-UTC convention. ``CURRENT_TIMESTAMP AT TIME ZONE 'UTC'`` yields a naive
  ``timestamp`` already in UTC, which is what the column expects.

This is the ONE place that knows the difference (constitution VI). Any code that
wants "the DB's idea of now" — to write it or to compare against it — uses
:func:`db_utc_now` rather than reaching for ``func.current_timestamp()`` /
``func.now()`` directly. The regression this guards against demoted every
session on the dashboard to "Status unknown" (2026-09-03) while both hosts were
beating normally; the Postgres-gated tests in ``server/tests/test_postgres_compat.py``
exercise the real writer/reader paths.
"""
from __future__ import annotations

from sqlalchemy import DateTime
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement


class _DbUtcNow(FunctionElement):
    """``CURRENT_TIMESTAMP`` normalised to a naive UTC ``DateTime`` per dialect."""
    type = DateTime()
    name = "db_utc_now"
    inherit_cache = True


@compiles(_DbUtcNow)
def _compile_default(element, compiler, **kw) -> str:  # noqa: ARG001 — SQLAlchemy hook signature
    # SQLite (and anything else without timestamptz semantics): already naive UTC.
    return "CURRENT_TIMESTAMP"


@compiles(_DbUtcNow, "postgresql")
def _compile_postgresql(element, compiler, **kw) -> str:  # noqa: ARG001
    # timestamptz → naive timestamp in UTC, independent of the session TimeZone.
    return "(CURRENT_TIMESTAMP AT TIME ZONE 'UTC')"


def db_utc_now() -> _DbUtcNow:
    """SQL expression for the database's current time as naive UTC.

    Usable both as a column value (``.values(beat_at=db_utc_now())``) and as a
    SELECT scalar (``select(db_utc_now())`` → naive ``datetime`` on every dialect).
    """
    return _DbUtcNow()
