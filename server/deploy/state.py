"""The fleet's durable "deployed version" (spec §1).

One row, id='singleton'. ``record_deploy`` is called by the controller once the edge is
healthy on the target — the moment the fleet's authoritative version moved, even if a
runner lagged (that runner is then fenced by the skew gate against the NEW sha).
``daemon_sha`` advances only for daemon-changing deploys: an edge-only deploy leaves
every runner process on its prior commit by design.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from server.db import db_session
from server.db.models import DeployStateRow

DEPLOY_STATE_ID = "singleton"


@dataclass(frozen=True)
class DeployState:
    deployed_sha: str
    deployed_short: str
    daemon_sha: str | None
    deployed_at: datetime | None


def _from_row(row: DeployStateRow | None) -> DeployState | None:
    if row is None or not row.deployed_sha:
        return None
    return DeployState(deployed_sha=row.deployed_sha,
                       deployed_short=row.deployed_short or row.deployed_sha[:7],
                       daemon_sha=row.daemon_sha, deployed_at=row.deployed_at)


async def read_deploy_state() -> DeployState | None:
    async with db_session() as db:
        row = await db.get(DeployStateRow, DEPLOY_STATE_ID)
        return _from_row(row)


async def record_deploy(*, sha: str, short: str, daemon_changed: bool,
                        by: str = "deploy-controller") -> DeployState:
    async with db_session() as db:
        row = await db.get(DeployStateRow, DEPLOY_STATE_ID)
        if row is None:
            row = DeployStateRow(id=DEPLOY_STATE_ID)
            db.add(row)
        row.deployed_sha = sha
        row.deployed_short = short
        if daemon_changed:
            row.daemon_sha = sha
        row.deployed_at = datetime.utcnow()
        row.deployed_by = by
        await db.flush()
        return _from_row(row)  # type: ignore[return-value]  # deployed_sha is set above
