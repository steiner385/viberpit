"""Classify a DB failure as a transient connectivity blip vs a real defect (#2752).

A periodic loop that reads the DB and degrades on failure (cached snapshot,
"unknown") should log the blip quietly — a WARNING files an auto-error issue —
and keep WARNING for what a human must fix (schema drift, bad SQL).
"""
from __future__ import annotations

import asyncio

from asyncpg.exceptions import PostgresConnectionError
from sqlalchemy.exc import DBAPIError

from server.aio import iter_exception_chain

# OSError covers ConnectionError/ConnectionRefusedError/ConnectionResetError and
# socket-level failures; TimeoutError is the pool-checkout / connect timeout.
_TRANSIENT_TYPES: tuple[type[BaseException], ...] = (
    asyncio.CancelledError, TimeoutError, OSError, PostgresConnectionError,
)


def is_transient_db_failure(exc: BaseException) -> bool:
    """True if *exc* (or anything it was raised from) is a connect-level failure.

    Covers the asyncpg/uvloop shape where a cancelled or timed-out TLS handshake
    on a fresh pool connection surfaces as a concrete exception ``from`` a
    ``CancelledError``, and SQLAlchemy's ``connection_invalidated`` wrapper.
    """
    for e in iter_exception_chain(exc):
        if isinstance(e, _TRANSIENT_TYPES):
            return True
        if isinstance(e, DBAPIError) and e.connection_invalidated:
            return True
    return False
