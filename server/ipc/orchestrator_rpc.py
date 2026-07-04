"""Expose a real SessionOrchestrator over the A1 daemon RPC/event transport.

Keeps orchestrator knowledge out of the generic transport (daemon.py). A2.1 wires
the vertical-slice methods; A2.2 adds the rest.
"""
import asyncio
from server.ipc.daemon import register_rpc
from server.sessions.types import SYSTEM_IDENTITY


def register_orchestrator_rpc(app, orch) -> None:
    # session_id -> the driver object whose events() a live pump task is forwarding.
    # Must track the DRIVER, not just the session_id: a driver can be torn down and
    # rebuilt (watchdog/token recompose, resume after detach) while the session_id is
    # unchanged. A plain set that was added-to but never cleared stranded the pump on
    # the dead driver forever — the replacement driver's events published nowhere and
    # the live transcript froze until a reattach forced a disk replay. Keying on driver
    # identity lets ensure_driver restart the pump for a swapped-in driver.
    app.state.pumped = {}

    async def _global_pump():
        q = orch.subscribe_events()
        while True:
            ev = await q.get()
            await app.state.publish("_global", ev)

    try:
        loop = asyncio.get_running_loop()
        app.state.global_pump = loop.create_task(_global_pump(), name="global-pump")
    except RuntimeError:
        app.state.global_pump = None   # no loop at register time (sync TestClient tests don't use the bus)

    async def _pump(session_id: str, driver) -> None:
        try:
            async for e in driver.events():
                await app.state.publish(session_id, e.to_wire())
        finally:
            # Self-clean so a future ensure_driver (reattach, or a new driver after a
            # recompose) can restart the pump. Only clear if we're still the registered
            # pump for this driver — a newer pump may already have replaced us.
            if app.state.pumped.get(session_id) is driver:
                del app.state.pumped[session_id]

    async def ensure_driver(params: dict) -> dict:
        session_id = params["session_id"]
        driver = await orch.ensure_driver(session_id, params.get("identity", SYSTEM_IDENTITY))
        if driver is None:
            return {"found": False}
        # (Re)start the pump whenever the live driver isn't the one we're already
        # pumping — first attach, OR a swapped-in driver after a teardown/recompose.
        # Same driver → no-op (avoids double-pumping, which would duplicate events).
        if app.state.pumped.get(session_id) is not driver:
            app.state.pumped[session_id] = driver
            asyncio.create_task(_pump(session_id, driver), name=f"pump-{session_id}")
        return {"found": True, "status": driver.current_status(), "queued": driver.queued(),
                "head_seq": app.state.head_seq(session_id),
                "resume_since": app.state.resume_since(session_id),
                "turn_active": app.state.turn_open_for(session_id)}

    async def list_sessions(params: dict) -> dict:
        return {"sessions": [r.to_dict() for r in orch.list_sessions()]}

    async def get_session(params: dict) -> dict:
        rec = orch.get_session(params["session_id"])
        return {"session": rec.to_dict() if rec is not None else None}

    async def create_session(params: dict) -> dict:
        rec = await orch.create_session(**params)
        return {"session": rec.to_dict()}

    async def send_prompt(params: dict) -> dict:
        status = await orch.send_prompt(params["session_id"], params["text"], params.get("images"))
        return {"status": status}

    async def pr_for(params: dict) -> dict:
        info = orch.pr_for(params.get("repo"), params.get("branch"))
        return {"pr": info.to_dict() if info is not None else None}

    async def prs_for(params: dict) -> dict:
        infos = orch.prs_for(params.get("session_id"))
        return {"prs": [i.to_dict() for i in infos]}

    import inspect

    async def all_prs(params: dict) -> dict:
        items = orch.all_prs()
        if inspect.isawaitable(items):
            items = await items
        return {"prs": [p.to_dict() for p in items]}

    async def all_repos_stage_models(params: dict) -> dict:
        models = orch.all_repos_stage_models()
        if inspect.isawaitable(models):
            models = await models
        return {"models": {k: v.to_dict() for k, v in models.items()}}

    async def all_merge_trains(params: dict) -> dict:
        trains = orch.all_merge_trains()
        if inspect.isawaitable(trains):
            trains = await trains
        return {"trains": {k: v.to_dict() for k, v in trains.items()}}

    async def check_insights(params: dict) -> dict:
        result = orch.check_insights(params["repo"], params.get("window", "7d"))
        if inspect.isawaitable(result):
            result = await result
        return result

    register_rpc(app, "ensure_driver", ensure_driver)
    register_rpc(app, "list_sessions", list_sessions)
    register_rpc(app, "get_session", get_session)
    register_rpc(app, "create_session", create_session)
    register_rpc(app, "send_prompt", send_prompt)
    register_rpc(app, "pr_for", pr_for)
    register_rpc(app, "prs_for", prs_for)
    async def pr_doctor_actionable(params: dict) -> dict:
        prs = await orch.pr_doctor_actionable()
        return {"prs": prs}

    register_rpc(app, "all_prs", all_prs)
    register_rpc(app, "all_repos_stage_models", all_repos_stage_models)
    register_rpc(app, "all_merge_trains", all_merge_trains)
    register_rpc(app, "check_insights", check_insights)
    register_rpc(app, "pr_doctor_actionable", pr_doctor_actionable)

    async def automation_actionable(params: dict) -> dict:
        items = await orch.automation_actionable(params["automation_id"])
        return {"items": items}

    register_rpc(app, "automation_actionable", automation_actionable)

    async def _maybe_await(r):
        return await r if inspect.isawaitable(r) else r

    # 24 methods whose return value is already JSON-safe (bool / None / str / list-of-dicts).
    _SIMPLE = [
        "archive_session", "rename_session", "set_autopilot_paused", "interrupt",
        "set_permission_mode", "cancel_queued", "resolve_permission", "answer_question",
        "transcript_path", "pending_questions", "pop_pending_initial_prompt",
        "pop_pending_initial_attachment_ids", "command_catalog", "run_command", "list_archived",
        "latest_suggestions", "generate_suggestions_now", "seed_idle_suggestions", "latest_summary",
        "begin_drain", "drain_status",
        "live_session_load", "live_session_counts",
        "snapshot_deploy", "restore_deploy", "resume_recovery_status",
        "request_pr_fix", "pr_doctor_fix", "backlog_runner_implement",
        "automation_statuses", "automation_trigger",
    ]

    def _make_simple(method_name):
        async def handler(params: dict):
            return await _maybe_await(getattr(orch, method_name)(**params))
        return handler

    for _m in _SIMPLE:
        register_rpc(app, _m, _make_simple(_m))

    async def permanent_delete(params: dict):
        result = await orch.permanent_delete(params["session_id"])
        app.state.evict(params["session_id"])
        return result

    register_rpc(app, "permanent_delete", permanent_delete)

    async def unarchive(params: dict) -> dict:
        rec = await orch.unarchive(**params)
        return {"session": rec.to_dict() if rec is not None else None}

    async def adopt_session(params: dict) -> dict:
        rec, metadata = await orch.adopt_session(**params)
        return {"record": rec.to_dict(), "metadata": metadata}

    async def transcript_events(params: dict) -> dict:
        events = await orch.transcript_events(**params)
        return {"events": [e.to_wire() for e in events]}

    register_rpc(app, "unarchive", unarchive)
    register_rpc(app, "adopt_session", adopt_session)
    register_rpc(app, "transcript_events", transcript_events)

    async def usage(params: dict) -> dict:
        u = await orch.poller.get(params["name"])
        return {"usage": u.to_dict() if u is not None else None}

    async def pending_permissions(params: dict):
        return orch._registry.pending(params["session_id"])

    async def active_session_count(params: dict):
        return orch.active_session_count()

    register_rpc(app, "usage", usage)
    register_rpc(app, "pending_permissions", pending_permissions)
    register_rpc(app, "active_session_count", active_session_count)
