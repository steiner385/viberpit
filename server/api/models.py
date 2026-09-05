"""GET /api/models — the model list for the session model selector, per engine.

One endpoint, two engines, and deliberately not two endpoints. The browser's model
picker is one component; giving it ``/api/models`` for Claude and
``/api/codex/models`` for Codex would put an ``if engine ===`` in the frontend and
guarantee the two lists drift in shape. So the route is engine-dispatched here and
the wire shape is a superset:

* ``{"id", "label"}`` — what the Claude catalog has always returned, and what every
  existing caller still gets when it omits ``engine``.
* plus ``{"description", "default", "reasoningEfforts", "defaultReasoningEffort",
  "inputModalities"}`` — the Codex controls, absent (not null-filled) for Claude.

Claude's list is host-independent and curated
(``server.sessions.model_catalog``). Codex's is a property of the *account*: it
comes from that account's ``model/list`` and differs by plan, so ``account`` is
required for ``engine=codex`` and meaningless for Claude.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from server.sessions import model_catalog
from server.sessions.engine import AgentEngine, InvalidAgentEngine, parse_agent_engine

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("")
async def get_models(
    request: Request,
    refresh: bool = False,
    engine: str | None = None,
    account: str | None = None,
) -> list[dict]:
    """Return the model list as ``[{"id", "label", ...}]``. No auth-sensitive data.

    ``?refresh=1`` re-probes the source (the claude binary, or the account's
    app-server) without restarting the server — e.g. after a CLI upgrade or a plan
    change.

    ``engine`` is optional and defaults to ``claude_code``: this route predates the
    engine registry and every existing caller omits it, so defaulting is the one
    ``legacy_default=True`` boundary here rather than a new required parameter.
    """
    try:
        selected = parse_agent_engine(engine, legacy_default=True)
    except InvalidAgentEngine as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    if selected is AgentEngine.CLAUDE_CODE:
        if refresh:
            model_catalog.refresh()
        return model_catalog.catalog()

    if not account:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "account_required",
                "message": (
                    "Codex model lists come from the selected account's app-server "
                    "and differ by plan, so an account is required."
                ),
            },
        )
    result = await request.app.state.sessions.codex_models(account, refresh=refresh)
    state = result.get("state")
    if state == "unavailable":
        # Not an error: Codex is simply not offered on this deployment. An empty
        # list lets the picker render "no models" instead of a failure toast.
        return []
    if state == "error":
        raise HTTPException(
            status_code=502,
            detail={"error": "codex_models_unavailable",
                    "message": result.get("detail") or "model/list failed"},
        )
    return list(result.get("models") or [])
