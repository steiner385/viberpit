"""Pipeline REST API — the full PR population for the board view.

GET /api/prs → every open + recently-merged PR across watched repos, each
joined to its authoring ccswitch session (or session=null). Requires a verified
identity. Reads the in-memory population cache (cheap); no GitHub call per request.

GET /api/pipeline/repos → per-repo StageModel (ordered nodes derived from
real CI/CD facts). Used by the frontend to render the horizontal stage tracks.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from server.auth.middleware import current_identity
from server.pipeline.dashboard_state import dashboard_state

_EVENTS_INTERVAL_S = 10.0

router = APIRouter(prefix="/api/prs", tags=["pipeline"])


@router.get("")
async def list_prs(
    request: Request,
    _identity: str = Depends(current_identity),
) -> list[dict]:
    orch = request.app.state.sessions
    items = await orch.all_prs()
    return [p.to_dict() for p in items]


repos_router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


@repos_router.get("/repos")
async def list_stage_models(
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    orch = request.app.state.sessions
    models = orch.all_repos_stage_models()
    if inspect.isawaitable(models):
        models = await models
    return {repo: m.to_dict() for repo, m in models.items()}


@repos_router.get("/trains")
async def list_merge_trains(
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    orch = request.app.state.sessions
    trains = await orch.all_merge_trains()
    return {repo: st.to_dict() for repo, st in trains.items()}


async def _state_frame(orch) -> str:
    prs = await orch.all_prs()
    trains = await orch.all_merge_trains()
    queues = await orch.latest_queue_entries()
    models = orch.all_repos_stage_models()
    if inspect.isawaitable(models):
        models = await models
    prs_by_repo: dict[str, list] = {}
    for p in prs:
        prs_by_repo.setdefault(p.pr.repo, []).append(p)
    has_deploy = {
        repo: any(n.kind == "deploy" for n in m.nodes) for repo, m in models.items()
    }
    state = dashboard_state(
        prs_by_repo, queues, trains, has_deploy,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    return f"data: {json.dumps(state)}\n\n"


@repos_router.get("/events")
async def pipeline_events(
    request: Request,
    _identity: str = Depends(current_identity),
) -> StreamingResponse:
    orch = request.app.state.sessions

    async def gen():
        while True:
            if await request.is_disconnected():
                return
            yield await _state_frame(orch)
            await asyncio.sleep(_EVENTS_INTERVAL_S)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ReadyMergeBody(BaseModel):
    repo: str
    number: int


@repos_router.post("/pr/ready-merge")
async def ready_merge(
    body: ReadyMergeBody,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    """Plotroom PrRow parity: mark a draft PR ready + arm squash auto-merge."""
    orch = request.app.state.sessions
    return await orch.ready_merge(body.repo, body.number)


@repos_router.get("/insights/checks")
async def check_insights(
    request: Request,
    repo: str,
    window: str = "7d",
    _identity: str = Depends(current_identity),
) -> dict:
    orch = request.app.state.sessions
    result = orch.check_insights(repo, window)
    if inspect.isawaitable(result):
        result = await result
    return result
