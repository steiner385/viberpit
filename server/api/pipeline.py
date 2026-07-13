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
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from server.auth.middleware import current_identity
from server.pipeline.dashboard_state import dashboard_state
from server.pipeline.deploy_status import compute_repo_deploy
from server.pipeline.group_history import (
    count_group_ejects,
    count_group_runs,
    group_run_samples,
)
from server.pipeline.merge_sim import eject_probability
from server.pipeline.notifier import TransitionNotifier
from server.pipeline.timeline import timelines_for, tracked_merged

log = logging.getLogger(__name__)

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


async def _build_state(orch) -> dict:
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
    sim_ctx = await _sim_ctx(list(prs_by_repo))
    deploy_ctx, timeline_ctx = await _deploy_ctx(orch, prs_by_repo)
    return dashboard_state(
        prs_by_repo, queues, trains, has_deploy,
        generated_at=datetime.now(timezone.utc).isoformat(),
        sim_ctx=sim_ctx,
        deploy_ctx=deploy_ctx,
        timeline_ctx=timeline_ctx,
    )


async def _deploy_ctx(
    orch, prs_by_repo: dict[str, list]
) -> tuple[dict[str, dict], dict[str, dict[int, dict]]]:
    """Per-repo deploy block + PrTimeline map, gathered once per frame (SPEC §3).

    For each configured + enabled repo: one ``tracked_merged`` store pass →
    ``compute_repo_deploy`` (env-health snapshot from the watcher cache) and,
    from the same rows, the timeline dicts for the merged PRs present in this
    frame. Repos without a deploy config (or disabled) are absent from both
    maps — the frame keeps the stage-model hasDeploy derivation for them.
    """
    configs = await orch.deploy_config_list()
    if not configs:
        return {}, {}
    env_cache = await orch.deploy_status()  # {repo: [{name, liveSha, reachable}, ...]}
    deploy_ctx: dict[str, dict] = {}
    timeline_ctx: dict[str, dict[int, dict]] = {}
    # Merged PR numbers actually present in this frame, per repo.
    merged_in_frame: dict[str, list[int]] = {}
    for repo, plist in prs_by_repo.items():
        nums = [p.pr.number for p in plist if (p.pr.merged or p.pr.state == "merged")]
        if nums:
            merged_in_frame[repo] = nums
    for repo, cfg in configs.items():
        if not cfg.get("enabled", True):
            continue
        rows = await tracked_merged(repo)               # one store pass per repo
        envs = env_cache.get(repo, [])
        deploy_ctx[repo] = compute_repo_deploy(rows, envs)
        frame_nums = merged_in_frame.get(repo)
        if frame_nums:
            timeline_ctx[repo] = await timelines_for(repo, frame_nums)
    return deploy_ctx, timeline_ctx


async def _sim_ctx(repos: list[str]) -> dict[str, dict]:
    """Per-repo mergeEtaSim inputs, gathered once per frame (SPEC §1).

    One set of store reads per repo (last-20 durations + 7-day run/eject counts
    → ejectProb), NOT per PR. ``since_iso`` is trailing-Z form to match
    group_history's stored ISO form (lexicographic ISO windowing).
    """
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    since_iso = week_ago.isoformat().replace("+00:00", "Z")
    ctx: dict[str, dict] = {}
    for repo in repos:
        samples = await group_run_samples(repo)
        if not samples:
            continue  # simulate_merge_eta would return None anyway; skip the reads
        runs = await count_group_runs(repo, since_iso)
        ejects = await count_group_ejects(repo, since_iso)
        ctx[repo] = {
            "duration_samples": samples,
            "eject_prob": eject_probability(runs, ejects),
        }
    return ctx


@repos_router.get("/events")
async def pipeline_events(
    request: Request,
    _identity: str = Depends(current_identity),
) -> StreamingResponse:
    orch = request.app.state.sessions
    notifier = TransitionNotifier()

    async def gen():
        while True:
            if await request.is_disconnected():
                return
            try:
                state = await _build_state(orch)
            except Exception:                 # carry-forward (b): one bad frame must
                log.exception("pipeline SSE frame build failed")  # not kill the feed
                await asyncio.sleep(_EVENTS_INTERVAL_S)
                continue
            yield f"data: {json.dumps(state)}\n\n"
            for ev in notifier.diff(state):
                yield f"event: notification\ndata: {json.dumps(ev)}\n\n"
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


# --------------------------------------------------------------------------- #
# Deploy environment config (SPEC §3) — GET/PUT /api/pipeline/deploy-config     #
# --------------------------------------------------------------------------- #


class DeployEnv(BaseModel):
    name: str
    healthUrl: str
    auto: bool = False
    shaKey: str | None = None

    @field_validator("healthUrl")
    @classmethod
    def _http_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("healthUrl must start with http:// or https://")
        return v


class DeployConfigBody(BaseModel):
    repo: str
    environments: list[DeployEnv]
    enabled: bool = True


@repos_router.get("/deploy-config")
async def deploy_config_list(
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    orch = request.app.state.sessions
    result = orch.deploy_config_list()
    if inspect.isawaitable(result):
        result = await result
    return result


@repos_router.put("/deploy-config")
async def deploy_config_put(
    body: DeployConfigBody,
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    orch = request.app.state.sessions
    envs = [e.model_dump(exclude_none=True) for e in body.environments]
    result = orch.deploy_config_put(body.repo, envs, body.enabled)
    if inspect.isawaitable(result):
        await result
    return {"ok": True}
