"""Expose a real SessionOrchestrator over the A1 daemon RPC/event transport.

Keeps orchestrator knowledge out of the generic transport (daemon.py). A2.1 wires
the vertical-slice methods; A2.2 adds the rest.
"""
import asyncio
import logging
from server.ipc.daemon import register_rpc
from server.obs.tasks import supervise
from server.sessions.types import SYSTEM_IDENTITY

log = logging.getLogger(__name__)


def register_orchestrator_rpc(app, orch) -> None:
    # session_id -> the driver object whose events() a live pump task is forwarding.
    # Must track the DRIVER, not just the session_id: a driver can be torn down and
    # rebuilt (watchdog/token recompose, resume after detach) while the session_id is
    # unchanged. A plain set that was added-to but never cleared stranded the pump on
    # the dead driver forever — the replacement driver's events published nowhere and
    # the live transcript froze until a reattach forced a disk replay. Keying on driver
    # identity lets ensure_driver restart the pump for a swapped-in driver — and lets the
    # orchestrator's on_driver_replaced hook (below) restart it for a RECOVERY swap
    # (rotation / 401 recompose / crash relaunch) that carries no RPC at all.
    app.state.pumped = {}
    # session_id -> (driver, consecutive-failure count). Keyed on driver identity so a
    # genuinely NEW driver object gets its own one-retry budget; the SAME driver failing
    # its restart trips the count to 2 → give up (bounded, no hot loop). See _on_pump_failed.
    app.state.pump_failures = {}
    # Lifetime consecutive-failure count for the single global pump.
    app.state.global_pump_failures = 0

    async def _global_pump():
        q = orch.subscribe_events()
        while True:
            ev = await q.get()
            await app.state.publish("_global", ev)

    def _on_global_pump_failed(exc: BaseException) -> None:
        # The global pump dying stops ALL lifecycle/status frames to every edge. Restart
        # ONCE; a second consecutive failure logs loudly and gives up (no hot loop).
        app.state.global_pump_failures += 1
        if app.state.global_pump_failures > 1:
            log.error("global pump failed %d× consecutively — giving up; lifecycle/status "
                      "frames to every edge have stopped", app.state.global_pump_failures)
            return
        log.warning("global pump failed — restarting once")
        _start_global_pump()

    def _start_global_pump() -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            app.state.global_pump = None   # no loop at register time (sync TestClient tests don't use the bus)
            return
        app.state.global_pump = supervise(
            loop.create_task(_global_pump(), name="global-pump"),
            name="global-pump", on_failure=_on_global_pump_failed)

    _start_global_pump()

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

    def _on_pump_failed(session_id: str, driver, exc: BaseException) -> None:
        # A pump whose driver.events() raised froze this session's live transcript.
        # Restart it ONCE on the same driver (the finally above already cleared its
        # registration). Per-driver consecutive counting: a NEW driver object resets to
        # 1 (fresh budget) while the SAME driver's restart failing trips 2 → give up.
        prev_driver, count = app.state.pump_failures.get(session_id, (None, 0))
        count = count + 1 if prev_driver is driver else 1
        app.state.pump_failures[session_id] = (driver, count)
        if count > 1:
            log.error("pump for session %s failed %d× consecutively — giving up; its live "
                      "transcript is frozen until a reattach", session_id, count)
            return
        # Only restart if nothing newer took over: a recovery swap installs a replacement
        # driver + its own pump via on_driver_replaced, so restarting the dead driver would
        # clobber it (and _ensure_pump would happily overwrite pumped[session_id]).
        if app.state.pumped.get(session_id) is None:
            log.warning("pump for session %s failed — restarting once", session_id)
            _ensure_pump(session_id, driver)

    def _ensure_pump(session_id: str, driver) -> None:
        # (Re)start the pump whenever the live driver isn't the one we're already
        # pumping — first attach, OR a swapped-in driver after a teardown/recompose.
        # Same driver → no-op (avoids double-pumping, which would duplicate events).
        # Single source of truth: called by the ensure_driver RPC (attach), the
        # orchestrator's on_driver_replaced hook (recovery swap with no RPC), and the
        # supervised one-retry restart in _on_pump_failed.
        if app.state.pumped.get(session_id) is not driver:
            app.state.pumped[session_id] = driver
            supervise(
                asyncio.create_task(_pump(session_id, driver), name=f"pump-{session_id}"),
                name=f"pump-{session_id}",
                on_failure=lambda exc: _on_pump_failed(session_id, driver, exc))

    # A recovery path (orchestrator._reassign) swaps a session's driver WITHOUT any RPC,
    # so ensure_driver never runs to restart the pump: the old driver's events() ends,
    # its pump self-cleans, and no new pump starts — freezing every already-attached
    # viewer's live transcript until they reattach. The orchestrator notifies us on each
    # swap; restart the pump on the new driver. Loop-safe: the orchestrator drives the
    # swap on this same event loop, so create_task inside _ensure_pump has a running loop.
    orch.on_driver_replaced = _ensure_pump

    async def ensure_driver(params: dict) -> dict:
        session_id = params["session_id"]
        driver = await orch.ensure_driver(session_id, params.get("identity", SYSTEM_IDENTITY))
        if driver is None:
            return {"found": False}
        _ensure_pump(session_id, driver)
        return {"found": True, "status": driver.current_status(), "queued": driver.queued(),
                "head_seq": app.state.head_seq(session_id),
                "resume_since": app.state.resume_since(session_id),
                "turn_active": app.state.turn_open_for(session_id),
                # Epoch seconds of the open turn's start (None when no turn is open).
                # The edge hands it to replay_window so the incomplete-turn drop only
                # trims the turn that actually IS in progress (resume-lag bug: the
                # disk's last user entry can belong to a PREVIOUS, completed turn).
                "turn_started_at": app.state.turn_started_at(session_id)}

    async def ring_covers(params: dict) -> dict:
        # Wave-3 cursor catch-up coverage probe: can the daemon ring replay this stream
        # from `since` (the browser's contiguous srcSeq cursor) with no GAP? Pure read of
        # the ring state — the edge (RemoteOrchestrator.ring_covers) decides resumeOk vs
        # full replay off the boolean, BEFORE anything is enqueued.
        #
        # `epoch` rides the SAME response (#497 stale-epoch RPC race): the edge's epoch gate
        # compares the cursor's minting epoch against the epoch this daemon reports RIGHT NOW,
        # not against the edge's cached epoch — which can be stale-but-non-None while the
        # events-reader is still in reconnect backoff after a daemon restart. Reporting it here
        # makes the gate authoritative at gate time; the coverage semantics are unchanged.
        covered = app.state.ring_covers(params["session_id"], params.get("since"))
        return {"covered": bool(covered), "epoch": app.state.epoch}

    async def ring_tail(params: dict) -> dict:
        # One-shot ring snapshot after `since` for the cursor catch-up: the edge replays
        # these (then continues on the shared live fan-out, deduping by seq). See
        # daemon.ring_tail for why a snapshot RPC rather than a since-override subscribe.
        return app.state.ring_tail(params["session_id"], params.get("since"))

    async def list_sessions(params: dict) -> dict:
        return {"sessions": [r.to_dict() for r in orch.list_sessions()]}

    async def get_session(params: dict) -> dict:
        rec = orch.get_session(params["session_id"])
        return {"session": rec.to_dict() if rec is not None else None}

    async def create_session(params: dict) -> dict:
        rec = await orch.create_session(**params)
        return {"session": rec.to_dict()}

    async def send_prompt(params: dict) -> dict:
        # `interrupt` (interrupt-and-post) is only forwarded when set, so an edge
        # that predates the flag keeps the plain queued-send contract.
        extra = {"interrupt": True} if params.get("interrupt") is True else {}
        status = await orch.send_prompt(params["session_id"], params["text"],
                                        params.get("images"), **extra)
        return {"status": status}

    async def pr_for(params: dict) -> dict:
        info = orch.pr_for(params.get("repo"), params.get("branch"))
        return {"pr": info.to_dict() if info is not None else None}

    async def prs_for(params: dict) -> dict:
        infos = orch.prs_for(params.get("session_id"))
        return {"prs": [i.to_dict() for i in infos]}

    import inspect

    async def prs_for_many(params: dict) -> dict:
        by_sid = orch.prs_for_many(params.get("session_ids") or [])
        if inspect.isawaitable(by_sid):
            by_sid = await by_sid
        return {"prs": {sid: [i.to_dict() for i in infos] for sid, infos in by_sid.items()}}

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

    async def latest_queue_entries(params: dict) -> dict:
        import dataclasses
        queues = orch.latest_queue_entries()
        if inspect.isawaitable(queues):
            queues = await queues
        return {"queues": {
            repo: [dataclasses.asdict(e) for e in entries]
            for repo, entries in queues.items()
        }}

    async def check_insights(params: dict) -> dict:
        result = orch.check_insights(params["repo"], params.get("window", "7d"))
        if inspect.isawaitable(result):
            result = await result
        return result

    async def ready_merge(params: dict) -> dict:
        result = orch.ready_merge(params["repo"], params["number"])
        if inspect.isawaitable(result):
            result = await result
        return result

    async def deploy_config_list(params: dict) -> dict:
        result = orch.deploy_config_list()
        if inspect.isawaitable(result):
            result = await result
        return {"configs": result}

    async def deploy_config_put(params: dict) -> dict:
        result = orch.deploy_config_put(
            params["repo"], params["environments"], params.get("enabled", True)
        )
        if inspect.isawaitable(result):
            await result
        return {"ok": True}

    async def deploy_status(params: dict) -> dict:
        result = orch.deploy_status()
        if inspect.isawaitable(result):
            result = await result
        return {"status": result}

    register_rpc(app, "ensure_driver", ensure_driver)
    register_rpc(app, "ring_covers", ring_covers)
    register_rpc(app, "ring_tail", ring_tail)
    register_rpc(app, "list_sessions", list_sessions)
    register_rpc(app, "get_session", get_session)
    register_rpc(app, "create_session", create_session)
    register_rpc(app, "send_prompt", send_prompt)
    register_rpc(app, "pr_for", pr_for)
    register_rpc(app, "prs_for", prs_for)
    register_rpc(app, "prs_for_many", prs_for_many)

    register_rpc(app, "all_prs", all_prs)
    register_rpc(app, "all_repos_stage_models", all_repos_stage_models)
    register_rpc(app, "all_merge_trains", all_merge_trains)
    register_rpc(app, "latest_queue_entries", latest_queue_entries)
    register_rpc(app, "check_insights", check_insights)
    register_rpc(app, "ready_merge", ready_merge)
    register_rpc(app, "deploy_config_list", deploy_config_list)
    register_rpc(app, "deploy_config_put", deploy_config_put)
    register_rpc(app, "deploy_status", deploy_status)

    async def automation_actionable(params: dict) -> dict:
        items = await orch.automation_actionable(params["automation_id"])
        return {"items": items}

    register_rpc(app, "automation_actionable", automation_actionable)

    async def _maybe_await(r):
        return await r if inspect.isawaitable(r) else r

    # Methods whose return value is already JSON-safe (bool / None / str / list-of-dicts).
    _SIMPLE = [
        "archive_session", "resume_interrupted", "reload_session", "rotate_to_best", "set_account_pin", "rename_session", "note_user_interaction", "set_autopilot_paused", "interrupt",
        "set_permission_mode", "cancel_queued", "resolve_permission", "answer_question",
        "transcript_path", "resolve_transcript_path", "transcript_search_text",
        "pending_questions", "orphaned_interaction", "pop_pending_initial_prompt",
        "pop_pending_initial_attachment_ids", "command_catalog", "run_command", "list_archived",
        "latest_suggestions", "latest_suggestions_many",
        "generate_suggestions_now", "seed_idle_suggestions",
        "latest_summary", "latest_summary_many",
        "begin_drain", "drain_status",
        "live_session_load", "live_session_counts",
        "snapshot_deploy", "restore_deploy", "resume_recovery_status", "startup_report",
        "request_pr_fix",
        "automation_statuses", "automation_trigger", "pr_doctor_job",
        "consolidation_candidates", "automation_consolidate",
        "backlog_candidates", "backlog_runner_dispatch",
        "copilot_add_start", "copilot_add_status", "copilot_add_cancel", "copilot_usage",
        "session_queue_move", "session_queue_cancel", "session_queue_positions", "maybe_start_queued",
        "git_credential_alerts", "dismiss_git_credential_alert",
        "list_adoptable_candidates", "migrate_archive_home", "cloned_repos",
        # pm onboarding seams (phase 2, 2026-08-31) — pm_classify_run returns int
        "pm_classify_run",
        # advance map_proposed → map_merged (returns None, JSON-safe)
        "pm_advance_map_merged",
    ]

    def _make_simple(method_name):
        async def handler(params: dict):
            return await _maybe_await(getattr(orch, method_name)(**params))
        return handler

    for _m in _SIMPLE:
        register_rpc(app, _m, _make_simple(_m))

    import dataclasses

    async def pm_onboarding_counts(params: dict) -> dict:
        # pm_onboarding_counts returns a ProjectOnboardingView frozen dataclass —
        # NOT JSON-native. Convert via dataclasses.asdict before the transport's
        # json.dumps sees it (the _SIMPLE path would raise TypeError here).
        view = await _maybe_await(orch.pm_onboarding_counts(**params))
        return dataclasses.asdict(view)

    async def pm_onboard_spawn_map(params: dict) -> dict | None:
        # pm_onboard_spawn_map returns a session-record OBJECT (or None) — also
        # not JSON-native. Strip to {"id": ...} which is all the API layer needs.
        rec = await _maybe_await(orch.pm_onboard_spawn_map(**params))
        if rec is None:
            return None
        return {"id": getattr(rec, "id", None)}

    register_rpc(app, "pm_onboarding_counts", pm_onboarding_counts)
    register_rpc(app, "pm_onboard_spawn_map", pm_onboard_spawn_map)

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

    async def transcript_window(params: dict) -> dict | None:
        # The daemon resolves the transcript path AND replays it in-process — the
        # edge (now potentially on a different host) never touches the path itself
        # (see server/api/sessions.py's _search_ids note on the cross-host hazard).
        window = await orch.transcript_window(**params)
        if window is None:
            return None
        # Events are real ev.* objects here (in-process replay); serialize the same
        # way transcript_events does. hasattr guards a future where orch.transcript_window
        # might already hand back wire dicts, matching api/sessions.py's own _wire().
        return {
            "events": [e.to_wire() if hasattr(e, "to_wire") else e for e in window.events],
            "earliestTurn": window.earliest_turn,
            "totalTurns": window.total_turns,
        }

    async def resolve_model_approval(params: dict) -> dict:
        # .get, not [] — an older client that omits "model" (pre-counter-offer)
        # must still work; None means "approve exactly what was requested".
        rec = await orch.resolve_model_approval(
            params["session_id"], params["decision"], model=params.get("model"),
            identity=params["identity"])
        return {"session": rec.to_dict()}

    register_rpc(app, "unarchive", unarchive)
    register_rpc(app, "adopt_session", adopt_session)
    register_rpc(app, "transcript_events", transcript_events)
    register_rpc(app, "transcript_window", transcript_window)
    register_rpc(app, "resolve_model_approval", resolve_model_approval)

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
