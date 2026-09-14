"""Expand a stopped predecessor's coverage in one sorted, fenced transaction."""
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from server.automations.ownership import (
    CanonicalTarget, OwnershipConflict, OwnershipTransaction, StaleOwnership, _lock_targets,
)
from server.db import db_session
from server.db.models import AutomationOperation


@asynccontextmanager
async def expanded_handoff(token: str, targets: Sequence[CanonicalTarget]) -> AsyncIterator[OwnershipTransaction]:
    """Yield transfer-only transaction; caller writes successor state in tx.db.

    Additional targets cannot be locked inside an existing owned context: that
    would invert lock order between overlapping consolidations. The predecessor
    retains its immutable historical target set; only its successor expands it.
    """
    async with db_session() as db:
        operation = await db.get(AutomationOperation, token)
        if operation is None or not operation.targets:
            raise StaleOwnership("unknown handoff predecessor")
        keys = sorted(set(operation.targets) | {target.key for target in targets})
        rows = await _lock_targets(db, keys)
        if any(row.operation_token != token for row in rows if row.key in operation.targets):
            raise StaleOwnership("handoff predecessor lost ownership")
        if any(row.operation_token is not None for row in rows if row.key not in operation.targets):
            raise OwnershipConflict("expanded handoff conflicts with existing work")
        await db.refresh(operation)
        # Temporary assignments remain invisible until transfer commits. They
        # permit the same invariant/proof validation as an ordinary transfer.
        for row in rows:
            row.operation_token = token
        tx = OwnershipTransaction(db, operation, rows)
        try:
            yield tx
            if operation.phase != "transferred":
                raise ValueError("expanded handoff must transfer ownership")
        finally:
            tx._open = False
