"""Session tool handlers. Each does ONE thing: read the record, or call
``record_feedback`` once. No business logic here (spec §12)."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from viberpit import brand
from server.mcp_spec import ToolArgError, ToolSpec, annotations_for, tool_err
from server.obs.metrics import session_tool_calls_total
from server.sessions.child_sessions import ChildSessionLimit
from server.sessions.completion import (FINISHED_STATE_CLAUSE, NO_PR_RESOLUTIONS,
                                        STATUS_ONLY_CLAUSE, parse_hand_back,
                                        parse_mark_no_pr)
from server.sessions.permissions import MODE_WIDTH
from server.sessions.feedback import CATEGORIES, IMPACTS, record_feedback, validate_item
from server.sessions.model_request import MODEL_TIERS, parse_request_model

from .context import SessionToolContext, context_snapshot
from .registry import SESSION_TOOL_SPECS, mounted_names

log = logging.getLogger(__name__)

#: The cross-signal framing every SIGNAL description opens with. One literal, three
#: descriptions: whichever tool the model is reading, it sees the other two and what
#: each does to the session. This is the distinction no single description could carry.
_THREE_WAYS = (
    "Every turn ends ONE of three ways: `mark_complete` (the objective is fully done "
    "and verified; the session will be treated as finished and may be archived without "
    "anyone reading further), `mark_no_pr` (finished, but no pull request is "
    "appropriate), or `hand_back` (you need a decision, review, approval or information "
    "from the user before the objective can be finished). "
)


def tool_ok(data: Any) -> dict[str, Any]:
    env = {"source": brand.SLUG, "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "data": data}
    return {"content": [{"type": "text", "text": json.dumps(env, separators=(",", ":"), ensure_ascii=False)}]}


async def _audit_tool_call(ctx: SessionToolContext, spec: ToolSpec, ok: bool, ms: float) -> None:
    """Best-effort `session.tool_call` audit row, attributed to the session owner with
    ``via="session"``. Own db_session: a tool runs inside the SDK loop, not a request."""
    try:
        from server.audit import record_audit
        from server.db import db_session
        async with db_session() as db:
            await record_audit(db, "session.tool_call", ctx.identity,
                               {"tool": spec.name, "class": spec.tool_class.value, "ok": ok,
                                "ms": round(ms), "session_id": ctx.session_id}, via="session")
            await db.commit()
    except Exception:  # noqa: BLE001 - an audit failure must never fail the tool
        log.warning("session.tool_call audit failed tool=%s", spec.name, exc_info=True)


def mounted_tool_names(ctx: SessionToolContext) -> tuple[str, ...]:
    """The tool names THIS context gets, in declaration order. A thin ctx-unpacking
    adapter over `registry.mounted_names` — THE predicate, also read by the
    system-prompt paragraph in `tools/instruction.py` — so `build_session_tools` and
    the paragraph can never disagree about which tool a session was declared."""
    return mounted_names(signalling_tools=ctx.signalling_tools,
                         self_model_switch=ctx.self_model_switch,
                         child_sessions=ctx.child_sessions)


def build_session_tools(ctx: SessionToolContext) -> list[SdkMcpTool[Any]]:
    def guarded(fn: Any) -> Any:
        spec = SESSION_TOOL_SPECS[fn.__name__]   # build-time KeyError for a spec-less handler

        async def run(args: dict[str, Any]) -> dict[str, Any]:
            t0 = time.monotonic()
            ok = True
            try:
                return tool_ok(await fn(args or {}))
            except ToolArgError as e:
                ok = False
                return tool_err(str(e))
            except Exception as e:  # noqa: BLE001 - never let a tool crash the SDK loop
                ok = False
                log.warning("session tool %s failed", spec.name, exc_info=True)
                return tool_err(f"{brand.NAME} tool error: {e.__class__.__name__}")
            finally:
                session_tool_calls_total.labels(spec.name, spec.tool_class.value,
                                                "ok" if ok else "error").inc()
                await _audit_tool_call(ctx, spec, ok, (time.monotonic() - t0) * 1000)
        return run

    def spec_tool(name: str, description: str, schema: dict[str, Any]) -> Any:
        return tool(name, description, schema, annotations=annotations_for(SESSION_TOOL_SPECS[name]))

    @spec_tool("session_context",
               f"What {brand.NAME} knows about THIS session: id, name, repo, branch, cwd, host, "
               "engine, model, account, the automation that dispatched it (origin) and its "
               "reference (originRef, e.g. owner/repo#123), the effective goal, the "
               "permission mode, and this session's lineage: parentSessionId and its "
               "children (id, name, status). Call it when you need to know how you were "
               "dispatched or how your child sessions are doing.",
               {"type": "object", "properties": {}, "required": []})
    @guarded
    async def session_context(args: dict[str, Any]) -> Any:
        return context_snapshot(ctx)

    @spec_tool("report_feedback",
               f"Report a problem with {brand.NAME}, the harness that dispatched and supervises "
               "this session — never a bug in the project you are working on or a choice the "
               f"user made. Call it when {brand.NAME}'s instructions contradict each other or the "
               "task, the dispatch prompt was wrong or ambiguous about the repo, branch or "
               f"issue, {brand.NAME} interrupted or switched model or account at a bad moment, or a "
               "worktree, credential, tool or marker it provides is broken. One call per "
               "distinct problem, at most three per session. It records the report for the "
               f"{brand.NAME} backlog and returns immediately — keep working. "
               f"category: one of {', '.join(CATEGORIES)}. impact: one of {', '.join(IMPACTS)}. "
               f"title: 8-120 chars. body: what happened, what you expected, what {brand.NAME} "
               "should change (quote the exact text when the problem is an instruction). "
               "evidence (optional): the offending prompt excerpt, command, or error.",
               {"type": "object",
                "properties": {"category": {"type": "string", "enum": list(CATEGORIES)},
                               "impact": {"type": "string", "enum": list(IMPACTS)},
                               "title": {"type": "string"}, "body": {"type": "string"},
                               "evidence": {"type": "string"}},
                "required": ["category", "impact", "title", "body"]})
    @guarded
    async def report_feedback(args: dict[str, Any]) -> Any:
        from server.config import get_settings
        item = validate_item(args)
        res = await record_feedback(session_id=ctx.session_id, identity=ctx.identity, item=item,
                                    context=context_snapshot(ctx),
                                    cap=get_settings().session_feedback_cap)
        return {"recorded": res.recorded, "status": res.status}

    @spec_tool("mark_complete",
               "Signal that you have FULLY accomplished the objective of this session. "
               + _THREE_WAYS +
               "Call this only when the work is genuinely complete — never for an interim "
               "step, never while you are still working, and never in the same turn as a "
               "question to the user: if you need an answer, call `hand_back` instead. "
               + FINISHED_STATE_CLAUSE +
               " Optional follow-ups belong in prose as statements ('I can also do X if "
               "wanted'), not questions.",
               {"type": "object", "properties": {}, "required": []})
    @guarded
    async def mark_complete(args: dict[str, Any]) -> Any:
        return {"acknowledged": True}

    @spec_tool("mark_no_pr",
               "Signal that the work is finished but NO pull request is appropriate. "
               + _THREE_WAYS +
               "Use this INSTEAD of mark_complete when finishing without a code change. "
               "resolution: 'not-needed' when the objective was already satisfied without "
               "one, 'cancelled' when the user called the work off or you deliberately "
               "decided not to open one, 'awaiting-decision' when you called "
               "`propose_decision` and the issue is parked on the person's answer. An "
               "objective that asked for a code change is NOT "
               "finished by investigating it — discovering the work is larger than expected, "
               "recommending a spec, or offering to continue is a hand-back: say what you "
               "found, ask how to proceed, and call `hand_back`, not this. Never call this in "
               "the same turn as a question to the user: if you need an answer, call "
               "`hand_back` instead. note: one short sentence.",
               {"type": "object",
                "properties": {"resolution": {"type": "string", "enum": list(NO_PR_RESOLUTIONS)},
                               "note": {"type": "string"}},
                "required": ["resolution"]})
    @guarded
    async def mark_no_pr(args: dict[str, Any]) -> Any:
        return {"acknowledged": True, **parse_mark_no_pr(args)}

    @spec_tool("hand_back",
               "Hand this session back to the user because you need something from them "
               "before the objective can be finished: a decision, a review, an approval, or "
               "information you do not have. reason: one sentence naming exactly what you "
               "need. "
               + _THREE_WAYS +
               "Ending your message with a question and calling nothing is NOT a hand-back: "
               "the session reads as calmly idle and nobody is alerted. "
               + STATUS_ONLY_CLAUSE +
               " Call `hand_back` in the same message as your question, and never in the "
               "same turn as `mark_complete` or `mark_no_pr`.",
               {"type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"]})
    @guarded
    async def hand_back(args: dict[str, Any]) -> Any:
        return {"acknowledged": True, **parse_hand_back(args)}

    @spec_tool("request_model",
               "Ask to run on a different model tier, when the work has turned out "
               "materially harder or easier than it looked. haiku: trivial/mechanical — "
               "typo fixes, config bumps, dependency pins. sonnet: one feature or bugfix "
               "with its tests. opus: hard multi-file work, tricky debugging, CI failures "
               "with unclear cause. fable: the hardest architectural or cross-cutting work. "
               "A cheaper tier applies on your next turn; a more expensive one is held for a "
               "human, who may approve it at a different tier. Never routine, never twice "
               "for the same conclusion, and never wait for the switch — keep working.",
               {"type": "object",
                "properties": {"model": {"type": "string", "enum": list(MODEL_TIERS)},
                               "reason": {"type": "string"}},
                "required": ["model", "reason"]})
    @guarded
    async def request_model(args: dict[str, Any]) -> Any:
        return {"acknowledged": True, **parse_request_model(args)}

    @spec_tool("set_hold",
               "Hold or release the issue this session was dispatched for, on the "
               f"{brand.NAME} roadmap. kind 'evidence': record that someone must reproduce "
               "or correlate before anyone builds; the hold is visible on the roadmap and "
               "the next pass over this issue reads it. kind 'stronger_model': record that "
               "you are under-powered for this issue; the next pass reads it when choosing "
               "a model. kind null: release the hold you set (e.g. you found the cause and "
               "wrote it into the issue). reason: one sentence, required. ref (optional): "
               "owner/repo#N, default this session's issue. Only these two kinds are yours "
               "to set; a hold that needs the owner is hand_back's job.",
               {"type": "object",
                "properties": {"kind": {"type": ["string", "null"],
                                        "enum": ["evidence", "stronger_model", None]},
                               "reason": {"type": "string"},
                               "ref": {"type": "string"}},
                "required": ["kind", "reason"]})
    @guarded
    async def set_hold(args: dict[str, Any]) -> Any:
        from server.audit import record_audit
        from server.db import db_session
        from server.pm.holds import apply_session_hold
        rec = ctx.record()
        ref = args.get("ref") or getattr(rec, "origin_ref", None)
        if not ref:
            raise ToolArgError("this session has no issue ref; pass ref=owner/repo#N")
        requested_kind = args.get("kind")
        try:
            async with db_session() as db:
                wi, released = await apply_session_hold(
                    db, ref=str(ref), kind=requested_kind, reason=str(args.get("reason") or ""),
                    session_id=ctx.session_id, now=datetime.now(timezone.utc))
                # Setting always audits; releasing only when something was actually held
                # (matching the overlay route's `if released is not None` guard in
                # server/api/pm.py) — a null-kind call on an already-unheld item is a
                # true no-op and gets no `pm.hold.*` row.
                if requested_kind is not None:
                    await record_audit(db, "pm.hold.set", ctx.identity,
                                       {"workItemId": wi.id, "kind": wi.hold_kind,
                                        "reason": wi.hold_reason, "actorClass": "agent",
                                        "session_id": ctx.session_id}, via="session")
                elif released is not None:
                    await record_audit(db, "pm.hold.release", ctx.identity,
                                       {"workItemId": wi.id, "kind": released,
                                        "reason": str(args.get("reason") or ""), "actorClass": "agent",
                                        "signal": "session",   # from holds.RELEASE_SIGNALS
                                        "session_id": ctx.session_id}, via="session")
        except ValueError as e:  # HoldPermissionError is a ValueError subclass
            raise ToolArgError(str(e)) from e
        return {"ref": ref, "holdKind": wi.hold_kind, "holdBy": wi.hold_by}

    @spec_tool("propose_decision",
               "Park this session's issue on a PRODUCT DECISION only a person can take: "
               "a genuine fork in the road where the options are all defensible and the "
               "choice is the user's to make (which of two behaviours is correct, which "
               "data model to commit to, what to do with existing rows). Draft 2-6 "
               "options, name the one you recommend, and say why; the person picks one "
               "in the inbox and the next session on this issue is told what was chosen. "
               "Do NOT call it for a question you can answer yourself by reading the "
               "code, for a missing repro (that is `set_hold` kind 'evidence'), or for "
               "anything you could simply do and mention. It needs the issue this "
               "session was dispatched for: with no such issue (a session the user "
               "started themselves, who is right there to answer) `hand_back` is the "
               "way to ask, and this call is refused. options: [{key, label, "
               "detail}] — key is a short slug, label a few words, detail one sentence. "
               "recommended_key: the key you would pick. rationale: why, in one or two "
               "sentences; it is shown to the person and quoted back to the next "
               "session. After a successful call, end the turn with "
               '`mark_no_pr("awaiting-decision", note=...)` — the work is not abandoned '
               "and this is not a hand_back: the issue is parked on the proposal until "
               "the decision is taken.",
               {"type": "object",
                "properties": {
                    "options": {"type": "array",
                                "items": {"type": "object",
                                          "properties": {"key": {"type": "string"},
                                                         "label": {"type": "string"},
                                                         "detail": {"type": "string"}},
                                          "required": ["key", "label"]}},
                    "recommended_key": {"type": "string"},
                    "rationale": {"type": "string"}},
                "required": ["options", "recommended_key", "rationale"]})
    @guarded
    async def propose_decision(args: dict[str, Any]) -> Any:
        from sqlalchemy.exc import IntegrityError

        from server.db import db_session
        from server.pm import decisions
        from server.pm.holds import SESSION_PREFIX, work_item_for_ref
        rec = ctx.record()
        # The session's OWN issue, resolved exactly as `set_hold` resolves it. No
        # `ref` argument: a decision parks the item this session was dispatched for,
        # and letting a model name a third-party issue would park work nobody asked
        # it about.
        ref = getattr(rec, "origin_ref", None)
        if not ref:
            raise ToolArgError(
                "this session has no issue ref, so there is nothing to park a decision "
                "on; raise the choice with hand_back instead")
        # The TYPE check only (the boundary the route's pydantic body covers): a
        # number or a bare string would reach `normalize_options`'s `for` as a
        # TypeError and come back to the model as an opaque tool error. Every
        # SEMANTIC rule — how many options, unique keys, the recommendation among
        # them — stays in the service below.
        options = args.get("options")
        if not isinstance(options, list):
            raise ToolArgError(
                "options must be a list of 2-6 objects, each {key, label, detail}")
        try:
            async with db_session() as db:
                wi = await work_item_for_ref(db, str(ref))
                # ONE writer (constitution VI): `decisions.propose` validates the
                # options, writes the proposal AND the `decision` hold in this unit of
                # work, and records both audit rows. Nothing about that lives here.
                proposal = await decisions.propose(
                    db, wi, options=options,
                    recommended_key=str(args.get("recommended_key") or ""),
                    rationale=str(args.get("rationale") or ""),
                    actor=f"{SESSION_PREFIX}{ctx.session_id}",
                    now=datetime.now(timezone.utc), via="session")
                # Read the id INSIDE the block: the commit at the context exit expires
                # the instance, and a later attribute read would need a live session.
                data = {"ref": ref, "proposalId": proposal.id, "status": proposal.status,
                        "next": 'end the turn with mark_no_pr("awaiting-decision", '
                                'note="<the question, in one sentence>")'}
        except IntegrityError as e:
            # The partial unique index is what actually enforces "one pending proposal
            # per item"; the service's friendly SELECT is only the first line. Same
            # refusal either way — ONE wording, from the service.
            raise ToolArgError(decisions.PENDING_CONFLICT_MESSAGE) from e
        except ValueError as e:      # DecisionError/DecisionConflict are ValueErrors
            raise ToolArgError(str(e)) from e
        return data

    @spec_tool("dispatch_session",
               "Start a CHILD session of this one: a separate session on the same repo, in "
               "its own worktree, owned by the same user, that the user can open, steer, "
               "approve and stop like any session. Use it for a unit of work the user must "
               "be able to interact with independently — a sub-task that will need its own "
               "decisions, review or a long runtime; keep using the Agent tool for quick, "
               "self-contained research. name (3-60 chars): the board title. prompt: the "
               "child's COMPLETE brief — it does not see this conversation. model "
               f"(optional, one of {', '.join(MODEL_TIERS)}): default is this session's model. "
               "permission_mode (optional, one of plan, interactive, auto, full-auto): never "
               "wider than this session's. Returns the child's id and URL immediately; you "
               "receive a <child-session-notification> prompt when the child completes, hands "
               "back or ends, carrying its last message. Call session_context to list your "
               "children and their status. Live children are capped and grandchildren are "
               "limited; a refusal comes back as the tool result.",
               {"type": "object",
                "properties": {"name": {"type": "string"}, "prompt": {"type": "string"},
                               "model": {"type": "string"}, "permission_mode": {"type": "string"}},
                "required": ["name", "prompt"]})
    @guarded
    async def dispatch_session(args: dict[str, Any]) -> Any:
        name = str(args.get("name") or "").strip()
        prompt = str(args.get("prompt") or "").strip()
        model = args.get("model")
        mode = args.get("permission_mode")
        if not (3 <= len(name) <= 60):
            raise ToolArgError("name must be 3-60 characters")
        if not prompt:
            raise ToolArgError("prompt must not be empty — it is the child's whole brief")
        if model is not None and model not in MODEL_TIERS:
            raise ToolArgError(f"model must be one of {', '.join(MODEL_TIERS)}")
        if mode is not None and mode not in MODE_WIDTH:
            raise ToolArgError("permission_mode must be one of plan, interactive, auto, full-auto")
        if ctx.dispatch_child is None:
            raise ToolArgError("child sessions are not available in this session")
        try:
            return await ctx.dispatch_child(name=name, prompt=prompt, model=model, permission_mode=mode)
        except ChildSessionLimit as e:
            raise ToolArgError(str(e)) from e
        except Exception as e:  # noqa: BLE001 - a refused/failed create (busy accounts, clone,
            # branch, model gate) must reach the model as its message, not a class name
            log.warning("dispatch_session: child create failed session=%s", ctx.session_id,
                        exc_info=True)
            raise ToolArgError(f"could not start the child session: {e}") from e

    by_name = {"session_context": session_context, "report_feedback": report_feedback,
               "mark_complete": mark_complete, "mark_no_pr": mark_no_pr,
               "hand_back": hand_back, "set_hold": set_hold, "request_model": request_model,
               "propose_decision": propose_decision, "dispatch_session": dispatch_session}
    return [by_name[name] for name in mounted_tool_names(ctx)]
