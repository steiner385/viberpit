"""Real database authority setup for tests exercising autopilot mutation seams."""
from server.automations.autopilot_policy import DecisionContext
from server.db import db_session
from server.db.models import Automation, Session
from server.sessions import primary_lease, session_lease


def decision_context(rec, *, stuck=None):
    last = getattr(rec, 'last_user_interaction_at', None)
    return DecisionContext(
        session_id=rec.id, status=rec.status, account=rec.account, repo=rec.repo,
        branch=getattr(rec, 'branch', None), effective_goal='test assistance',
        goal_source='explicit', stuck=stuck or {'kind': rec.status},
        last_human_interaction_at=last.isoformat() if last is not None else None,
        origin=getattr(rec, 'origin', None))


async def seed_authority(orch):
    """After init_db, seed actual rows and matching leases; no safety seam is mocked."""
    from server.sessions.orchestrator import daemon_role, self_host_name

    async with db_session() as db:
        automation = await db.get(Automation, 'autopilot')
        if automation is None:
            db.add(Automation(id='autopilot', enabled=True, mode='auto', config={}))
        else:
            automation.enabled, automation.mode = True, 'auto'
        for rec in orch._records.values():
            row = await db.get(Session, rec.id)
            if row is None:
                row = Session(id=rec.id, name=rec.name, account_name=rec.account,
                              cwd=rec.cwd, status=rec.status, repo=rec.repo,
                              host=getattr(rec, 'host', None))
                db.add(row)
            else:
                row.status, row.host = rec.status, getattr(rec, 'host', None)
    if daemon_role() == 'primary':
        orch._lease_fence = await primary_lease.acquire(self_host_name(), ttl_s=300)
    for sid in getattr(orch, '_drivers', {}):
        fence = await session_lease.acquire(sid, self_host_name(), ttl_s=120)
        orch._lease_fences()[sid] = fence
