"""Exactly-once PR attempt accounting shared by callbacks and durable observers."""
from server.automations.ownership import OwnershipTransaction, stopped_work_attested
from server.db.models import AutomationTargetState, Session


async def account(tx: OwnershipTransaction, target: str) -> bool:
    """Commit admitted attempt from durable evidence, including a lost callback.

    The intent is stored before the external boundary. Accounting and release
    share the same target lock transaction, so an observer cannot discard it.
    """
    intent = tx.operation.provenance.get("pr_attempt")
    if not intent or intent.get("accounted"):
        return True
    kind = intent["kind"]
    if kind == "native":
        if not tx.operation.session_id and tx.operation.reserved_session_id:
            operation = tx.operation
            session = await tx.db.get(Session, operation.reserved_session_id)
            if (session is not None and session.origin == operation.owner == "pr-doctor"
                    and session.origin_ref == operation.provenance.get("origin_ref") == target
                    and operation.phase in ("launching", "uncertain")
                    and stopped_work_attested(operation)):
                # A lost bind response cannot erase the persisted launch. The
                # reserved UUID, actor provenance and current stopped generation
                # jointly prove this association; status alone never does.
                tx.bind_session(session.id)
        accepted = bool(tx.operation.session_id)
    else:
        outcome = tx.operation.provenance.get("mechanical_outcome")
        if outcome == "rejected":
            accepted = False
        elif outcome in ("accepted", "completed") or tx.operation.phase == "completed":
            accepted = True
        else:
            return False
    row = await tx.db.get(AutomationTargetState, ("pr-doctor", target))
    if row is None or row.state != intent["expected"]:
        return False
    if kind == "native" and not accepted:
        return False
    state = dict(intent["result"] if accepted else intent["expected"])
    if kind == "native":
        state["session_id"] = tx.operation.session_id
    row.state = state
    tx.operation.provenance = {**tx.operation.provenance,
                               "pr_attempt": {**intent, "accounted": True}}
    return True


async def detach(tx: OwnershipTransaction) -> bool:
    """Settle and detach a PR predecessor when a different workflow takes over.

    Target locks fence the operation; the PR row lock preserves the exact current
    history snapshot. A newer row's token is never cleared by an old operation.
    The caller must transfer/release in this same transaction.
    """
    from sqlalchemy import select

    if tx.operation.owner != "pr-doctor":
        return True
    target = tx.operation.provenance.get("origin_ref")
    if not target:
        return False
    row = (await tx.db.scalars(select(AutomationTargetState).where(
        AutomationTargetState.automation_id == "pr-doctor",
        AutomationTargetState.target == target,
    ).with_for_update())).one_or_none()
    if row is None or row.state.get("ownership_token") != tx.token:
        intent = tx.operation.provenance.get("pr_attempt")
        return not intent or bool(intent.get("accounted"))
    if not await account(tx, target):
        return False
    row.state = {key: value for key, value in row.state.items()
                 if key not in ("ownership_token", "session_id")}
    return True
