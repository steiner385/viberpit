from __future__ import annotations
import asyncio, logging, os, uuid as _uuid
from pathlib import Path
from typing import TYPE_CHECKING

from viberpit import paths as _paths
from server.mcp_spec import is_mcp_tool
from server.permission_rules.patterns import NEVER_RULED_TOOLS, unwrap_shell

if TYPE_CHECKING:
    from server.permission_rules.gate import StandingRuleGate

log = logging.getLogger(__name__)

# File-writing tools whose target is a single path argument.
_FILE_WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# ViberPit session modes whose names mirror the Claude Agent SDK's own
# ``permission_mode`` vocabulary. For these the SDK's permission engine is the
# front-line authority (it can auto-accept edits / bypass checks before the
# ``can_use_tool`` callback even fires); every other ViberPit mode
# (interactive/auto/allowlist/full-auto) runs the SDK on ``"default"`` and is
# gated entirely by ``PermissionRegistry.request`` below.
_SDK_PASSTHROUGH_MODES = frozenset({"plan", "acceptEdits", "bypassPermissions"})

#: How much a ViberPit permission mode lets a session do without asking — the ONE
#: ordering for "is mode A wider than mode B". A child session may never run wider
#: than its parent (child-session spec §4.3); an unknown mode ranks widest so it can
#: never be picked as the "narrower" of two.
MODE_WIDTH: dict[str, int] = {"plan": 0, "interactive": 1, "auto": 2, "full-auto": 3}


def narrowest(requested: str | None, parent: str) -> str:
    """The narrower of a requested mode and the parent's mode; the parent's when
    nothing was requested or the request is unknown."""
    if not requested:
        return parent
    widest = max(MODE_WIDTH.values()) + 1
    return requested if MODE_WIDTH.get(requested, widest) <= MODE_WIDTH.get(parent, widest) else parent


def to_sdk_permission_mode(mode: str) -> str:
    """Map a ViberPit session mode to the Claude Agent SDK ``permission_mode``.

    Single source of truth for the mapping: both the initial spawn
    (``claude_sdk_factory.real_sdk_factory``) and live mode switches
    (``ClaudeAgentDriver.set_permission_mode``) route through here so the two can
    never drift — the bug that motivated issue #750, where the mapping was
    duplicated verbatim and a new mode would have been honoured by one path and
    silently collapsed to ``"default"`` by the other.

    Modes that share the SDK's own vocabulary (``plan``/``acceptEdits``/
    ``bypassPermissions``) pass through verbatim; everything else uses the SDK's
    ``"default"`` engine and is gated by ``PermissionRegistry`` via the
    ``can_use_tool`` callback.
    """
    return mode if mode in _SDK_PASSTHROUGH_MODES else "default"


async def resolve_permission_mode(db, *, explicit: str | None = None,
                                  account: str | None = None) -> str:
    """The session mode a new session should start in. Single source of truth
    for the precedence chain, shared by every path that materialises a session
    (``POST /api/sessions`` and ``SessionOrchestrator.adopt_session``):

        explicit request value
        → the pinned account's own ``defaultPermissionMode`` (ledger)
        → the global Settings-page default (``AppSettings.default_permission_mode``)
        → ``"interactive"``

    The global step is what the DispatchBar quick-create bar and adopted
    sessions relied on and never got: each hardcoded ``"interactive"`` after the
    account step, so a "Full auto" operator still saw a permission prompt on
    every tool call. ``db`` is an open async session; the settings row is read,
    never created.
    """
    if explicit:
        return explicit
    if account:
        from viberpit import ledger as _ledger
        with _ledger.locked_ledger() as led:
            acct_mode = _ledger.get_account_defaults(led, account).get("defaultPermissionMode")
        if acct_mode:
            return acct_mode
    from sqlalchemy import select as _select
    from server.db.models import AppSettings
    row = (await db.execute(
        _select(AppSettings).where(AppSettings.id == "singleton"))).scalar_one_or_none()
    return (row.default_permission_mode if row is not None else None) or "interactive"


def _is_account_memory_path(file_path) -> bool:
    """True if file_path targets a managed-account auto-memory dir.

    Layout: <cfg_root>/<account>/projects/<encoded-cwd>/memory/**.  The auto-memory
    feature derives its directory from CLAUDE_CONFIG_DIR (= cfg/<account>), so the tool
    receives this literal cfg-anchored path — NOT the symlink-resolved ~/.claude/projects
    target.  Match lexically (os.path.abspath, no symlink resolution) and anchor on
    cfg_root() so this can never auto-approve a write into a real repository.
    """
    if not file_path:
        return False
    try:
        rel = Path(os.path.abspath(file_path)).relative_to(os.path.abspath(_paths.cfg_root()))
    except ValueError:
        return False
    parts = rel.parts  # (<account>, "projects", <encoded-cwd>, "memory", ...)
    return len(parts) >= 5 and parts[1] == "projects" and parts[3] == "memory"


class PermissionRegistry:
    """
    Lifecycle invariant: pending permission Futures are resolved to False ONLY by
    resolve(...) or deny_all(session_id). Callers MUST call deny_all on WebSocket
    disconnect, interrupt, and session close, or a tool's can_use_tool callback awaits
    forever. There is intentionally no timeout (human-in-the-loop approval may take a while).

    That "no timeout" is only safe while every way a human can respond actually reaches
    the registry. Sending a prompt is one of those ways — ``ClaudeAgentDriver.send_prompt``
    resolves a parked prompt through ``deny_all(reason=...)`` so the user's words reach
    the model — because a message typed at a parked turn would otherwise queue behind a
    turn that cannot end until this very prompt is answered (#1504).
    """
    DANGEROUS = frozenset({"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch"})
    # Tools that only read filesystem/index state and produce no side-effects.
    # Used by the "allowlist" mode ("Allowlist read-only, prompt the rest") to decide
    # which tools to auto-approve; everything else — including TodoWrite — must prompt.
    READ_ONLY = frozenset({"Read", "Grep", "Glob", "LS"})

    def __init__(self):
        self._pending: dict[str, dict] = {}              # perm_id -> {future, identity, session_id, tool, input, turn_id}
        # Set by the orchestrator. None = no standing rules (bare registries in tests).
        self.rule_gate: StandingRuleGate | None = None
        # perm_id -> (session_id, reason) a deny_all(reason=...) settled it with, read
        # back exactly once by the awaiting can_use_tool (see take_deny_reason).
        self._deny_reasons: dict[str, tuple[str, str]] = {}
        # session_id -> how many times deny_all has run for it while a standing-rule check
        # was in flight. ``request`` snapshots it before awaiting the gate: a deny_all
        # landing during that await (interrupt, disconnect, a prompt sent at the parked
        # turn) found no card to settle, so the request must not create one afterwards
        # (#1504 shape). Only sessions with a check in flight (``_gate_inflight``) have an
        # entry; the last check out drops both, so neither grows with every session seen.
        self._deny_gen: dict[str, int] = {}
        self._gate_inflight: dict[str, int] = {}
        # The user's approvals to mark one draft PR ready (``draft_hold_guard``),
        # written ONLY by ``resolve`` below — the one place a human's Allow lands.
        from server.sessions.draft_hold_guard import DraftReadyGrants
        self.draft_ready_grants = DraftReadyGrants()

    def _pend(self, session_id, turn_id, tool, input, *, mode, identity, always_allowed,
              items, parent_tool_use_id=None) -> tuple[str, asyncio.Future]:
        """Register a card and log it. The ONE place a pending record is created, so
        every card — destructive ViberPit tool, mutation tool, or an ordinary tool the
        session mode gates — leaves the same line naming the mode the registry saw.
        That line is what distinguishes "the operator chose a prompting mode" from
        "a full-auto driver was rebuilt as interactive" (2026-09-07), which no other
        signal exposes: the card itself looks identical either way."""
        suggestions = self._suggestions(session_id, tool, input, always_allowed)
        pid = _uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending[pid] = {"future": fut, "identity": identity, "session_id": session_id,
                              "tool": tool, "input": input, "turn_id": turn_id,
                              "always_allowed": always_allowed, "items": items,
                              "parent_tool_use_id": parent_tool_use_id,
                              "suggestions": suggestions}
        log.info("permission.card session=%s tool=%s mode=%s perm_id=%s always_allowed=%s",
                 session_id, tool, mode, pid, always_allowed)
        return pid, fut

    def _suggestions(self, session_id, tool, input, always_allowed) -> list[dict]:
        if self.rule_gate is None or not always_allowed or tool in NEVER_RULED_TOOLS:
            return []
        return self.rule_gate.suggestions(session_id, tool, input)

    async def _rule_allows(self, session_id, tool, input) -> bool | None:
        """True when a standing rule covers the request, False when not, None when a
        deny_all for *session_id* ran while the gate was being consulted (the caller
        must then settle the request denied without creating a card)."""
        if self.rule_gate is None:
            return False
        gen = self._deny_gen.get(session_id, 0)
        self._gate_inflight[session_id] = self._gate_inflight.get(session_id, 0) + 1
        try:
            hit = await self.rule_gate.match(session_id, tool, input)
            denied_meanwhile = self._deny_gen.get(session_id, 0) != gen
        finally:
            left = self._gate_inflight[session_id] - 1
            if left:
                self._gate_inflight[session_id] = left
            else:
                del self._gate_inflight[session_id]
                self._deny_gen.pop(session_id, None)
        if denied_meanwhile:
            return None
        return hit is not None

    def suggestions_for(self, perm_id) -> list[dict]:
        """Standing-rule patterns the card for *perm_id* offers (empty once resolved)."""
        rec = self._pending.get(perm_id)
        return list(rec.get("suggestions") or []) if rec else []

    async def request(self, session_id, turn_id, tool, input, *, mode, denylist, identity,
                      items: list[str] | None = None, parent_tool_use_id: str | None = None):
        # Our own MCP server (`mcp__viberpit__*`, and `mcp__ccswitch__*` from
        # before the rebrand) is the app talking to
        # itself, not a third-party integration trusted unconditionally like the
        # generic `mcp__` branch below — spec §4.4. Its cards are gated on the
        # tool's ANNOTATION (read/mutation/destructive), not on session mode, so
        # this branch sits ABOVE both the full-auto/bypassPermissions shortcut and
        # the generic mcp__ auto-allow: a destructive tool must prompt
        # even in full-auto. `is_mcp_tool` accepts BOTH brand prefixes, so a
        # resumed thread or a replayed transcript naming a tool the pre-rebrand
        # way is still gated by its annotation instead of falling through to the
        # generic `mcp__` auto-allow.
        #
        # The lookup goes through `server.mcp_spec`, which consults EVERY
        # registered surface — the assistant drawer AND a coding session's own
        # tools. Reading `server.assistant.tools.annotation_for` here instead
        # would resolve only the drawer's table, so a session tool would arrive
        # with a None annotation. Both surface modules are imported (lazily, to
        # keep the module-load order free of a server.assistant ↔ server.sessions
        # edge) purely for their `register_surface` side effect: an unregistered
        # surface answers None, and None fails OPEN into the mutation branch.
        if is_mcp_tool(tool):
            from server.assistant import tools as _assistant_tools  # noqa: F401 - registers surface
            from server.mcp_spec import ToolClass, annotation_for
            from server.sessions.tools import registry as _session_registry  # noqa: F401 - registers surface
            ann = annotation_for(tool)
            # READ and SIGNAL both auto-allow with no card. They differ in what they
            # claim, not in how they are gated: READ changes nothing, SIGNAL changes
            # only ViberPit's view of THIS session and replaces an ungated marker.
            if ann is not None and ann.tool_class in (ToolClass.READ, ToolClass.SIGNAL):
                return None, _resolved(True), True
            if ann is not None and ann.tool_class == ToolClass.DESTRUCTIVE:
                # Never consults standing rules, in ANY mode — a destructive
                # ViberPit action always surfaces a card, and that card never
                # offers "Always allow" (always_allowed=False on the pending
                # record; resolve() below refuses to persist a grant for it).
                pid, fut = self._pend(session_id, turn_id, tool, input, mode=mode,
                                      identity=identity, always_allowed=False, items=items,
                                      parent_tool_use_id=parent_tool_use_id)
                return pid, fut, False
            # MUTATION, or an unknown/not-yet-registered ViberPit tool (treated
            # conservatively as mutation-like rather than raising on a None
            # annotation — forward-compatible with tools Task 3.3 adds later).
            # Standing rules are honoured in EVERY mode, INCLUDING "interactive": a rule
            # is an explicit human decision (spec 2026-10-11 §3.1).
            # Tools that widen what sessions may do unasked (NEVER_RULED_TOOLS) skip it.
            allowed = (await self._rule_allows(session_id, tool, input)
                       if tool not in NEVER_RULED_TOOLS else False)
            if allowed is not False:  # True: a rule covers it; None: denied meanwhile
                return None, _resolved(allowed is True), True
            pid, fut = self._pend(session_id, turn_id, tool, input, mode=mode,
                                  identity=identity, always_allowed=True, items=items,
                                  parent_tool_use_id=parent_tool_use_id)
            return pid, fut, True
        if mode in ("full-auto", "bypassPermissions"):
            # Full auto / bypass-permissions — incl. shell: approve everything
            # without a prompt. ``bypassPermissions`` mirrors the SDK's own name
            # (the SDK bypasses its checks too), but the registry stays the
            # authority so the outcome is identical whether or not the SDK routes
            # the call through can_use_tool.
            return None, _resolved(True), True
        # Auto-allow the agent's own auto-memory writes (managed-account memory dir).
        # Honoured in EVERY mode, including "interactive": these writes are sandboxed
        # to cfg_root() (see _is_account_memory_path) and can never reach a real repo,
        # so they are pure bookkeeping noise the user should never have to approve.
        # Unlike a standing rule, "ask each time" does not apply here —
        # there is no user-facing action to ask about.
        if tool in _FILE_WRITE_TOOLS and _is_account_memory_path(
                input.get("file_path") or input.get("notebook_path")):
            return None, _resolved(True), True
        # Auto-allow MCP tools (`mcp__<server>__<tool>`) in EVERY mode, including
        # "interactive".  The operator opted into trusting MCP unconditionally, so a
        # configured MCP server's tools are never gated here.  (The binary's separate
        # project-`.mcp.json` server-trust gate is suppressed in sdk_factory via
        # enableAllProjectMcpServers — this only governs ViberPit's own per-tool card.)
        # ViberPit's own `mcp__ccswitch__*` tools are intercepted above and never
        # reach this branch.
        if tool.startswith("mcp__"):
            return None, _resolved(True), True
        if mode == "auto" and tool not in denylist:
            # Auto-approve safe tools: approve anything not on the dangerous denylist.
            return None, _resolved(True), True
        if mode == "allowlist" and tool in self.READ_ONLY:
            # Allowlist read-only, prompt the rest: only strictly read-only tools are
            # auto-approved; write-capable tools (TodoWrite, etc.) must prompt even if
            # they are not on the DANGEROUS denylist.
            return None, _resolved(True), True
        if mode == "acceptEdits" and tool in _FILE_WRITE_TOOLS:
            # Auto-accept file edits (Edit/Write/MultiEdit/NotebookEdit); every other
            # tool still prompts. Mirrors the SDK's acceptEdits semantics and is the
            # default for unattended automation sessions (issue #750): an agent editing
            # its own worktree proceeds without a human, while shell/network calls — a
            # materially larger blast radius — remain gated.
            return None, _resolved(True), True
        # A standing "always allow" rule is checked in EVERY mode, including interactive:
        # it is an explicit human decision (spec 2026-10-11 §3.1), unlike the old
        # per-session grant cache that "ask each time" overrode. Only reached where a
        # card would otherwise be created.
        if tool not in NEVER_RULED_TOOLS:
            allowed = await self._rule_allows(session_id, tool, input)
            if allowed is not False:  # True: a rule covers it; None: denied meanwhile
                return None, _resolved(allowed is True), True
        pid, fut = self._pend(session_id, turn_id, tool, input, mode=mode,
                              identity=identity, always_allowed=True, items=items,
                              parent_tool_use_id=parent_tool_use_id)
        return pid, fut, True

    async def request_external(self, session_id, turn_id, tool, input, *, identity,
                               always_allowed: bool = True):
        """Register a prompt an ENGINE already decided needs a human, and return
        ``(perm_id, future)`` where the future settles to the raw ViberPit
        decision string (``"allow"`` / ``"deny"`` / ``"always"``) — or ``False``
        if it was dismissed by :meth:`deny_all`.

        When a standing always-allow rule covers the request no card is created:
        ``perm_id`` is None and the future is already ``"allow"`` (or ``False`` when
        a deny_all ran during the check). Rules are matched on the script inside the
        engine's shell wrapper (``/bin/bash -lc 'npm test'`` is ``npm test``), and
        the card offers the same rule suggestions an ordinary prompt does.

        Two differences from :meth:`request`, both forced by who is deciding:

        * **No mode-based auto-approval.** ``request`` re-derives "does this
          need a human?" from the ViberPit mode because the Claude SDK asks about
          every tool. An external engine asks only when *its own* policy already
          concluded a human is required, and that policy was itself derived from
          the same ViberPit mode (``server/codex/permissions.py``). Re-applying
          the mode here would auto-answer a question the engine only raises in
          the modes where ViberPit wanted the prompt — and it would answer it
          ``allow``. A standing rule is different: it is an explicit, durable
          human decision, honoured in every mode, exactly as in :meth:`request`.
        * **The future carries the decision, not a boolean, and "always" never
          writes a ViberPit-side rule here.** The edge turns a chooser grant into a
          visible, revocable standing rule before resolving the card with
          ``"allow"``; a plain ``"always"`` that reaches the registry is mapped by
          the caller onto the engine's own SESSION-SCOPED accept.

        Everything else is deliberately shared with ordinary prompts: the same
        ``perm_id`` namespace, the same identity ownership check in
        :meth:`resolve`, the same :meth:`pending` / :meth:`rethread_identity` /
        :meth:`deny_all` semantics, and therefore the same WebSocket frames, REST
        route and UI card. There is no parallel approval registry.
        """
        rule_input = _rule_input(tool, input)
        if always_allowed and tool not in NEVER_RULED_TOOLS:
            allowed = await self._rule_allows(session_id, tool, rule_input)
            if allowed is not False:  # True: a rule covers it; None: denied meanwhile
                settled: asyncio.Future = asyncio.get_running_loop().create_future()
                settled.set_result("allow" if allowed else False)
                return None, settled
        pid = _uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[pid] = {"future": fut, "identity": identity, "session_id": session_id,
                              "tool": tool, "input": input, "rule_input": rule_input,
                              "turn_id": turn_id, "always_allowed": always_allowed,
                              "raw_decision": True,
                              "suggestions": self._suggestions(session_id, tool, rule_input,
                                                               always_allowed)}
        return pid, fut

    def resolve(self, perm_id, decision, identity) -> bool:
        rec = self._pending.get(perm_id)
        if rec is None:
            return False
        if rec["identity"] != identity:
            # Same diagnosis line as QuestionRegistry.resolve — the two identities
            # are what explain a "refused, but still pending" card.
            log.warning("permission.decision refused: identity mismatch session=%s perm=%s "
                        "owner=%s decider=%s", rec["session_id"], perm_id,
                        rec["identity"], identity)
            return False
        del self._pending[perm_id]
        raw = rec.get("raw_decision", False)
        if decision == "always" and not raw:
            # "Always" on a ViberPit-gated prompt allows THIS call only. The edge turned a
            # standing-rule "always" into a stored rule before calling here (spec §3.4);
            # nothing is cached in the registry, so a destructive tool or an unsafe
            # pattern simply asks again next time.
            decision = "allow"
        if decision in ("allow", "always"):
            self._record_draft_ready_grant(rec)
        if not rec["future"].done():
            rec["future"].set_result(decision if raw else decision == "allow")
        return True

    def _record_draft_ready_grant(self, rec: dict) -> None:
        """A human allowed a ``request_draft_ready`` card: grant that one PR to that
        one session. Keyed on the card's own record, never on anything the session
        says afterwards, so the model cannot write itself a grant."""
        from viberpit.brand import mcp_tool_stem
        from server.sessions.draft_hold_guard import REQUEST_TOOL, parse_ready_request
        if mcp_tool_stem(rec["tool"]) != REQUEST_TOOL:
            return
        target = parse_ready_request(rec["input"])
        if target is not None:
            self.draft_ready_grants.grant(rec["session_id"], *target)
            log.info("permission.draft_ready_grant session=%s pr=%s#%s",
                     rec["session_id"], *target)

    def pending(self, session_id) -> list[dict]:
        return [{"permId": pid, "tool": r["tool"], "input": r["input"],
                 # What standing rules match: the input, or the script inside an
                 # engine's shell wrapper (request_external).
                 "rule_input": r.get("rule_input") or r["input"], "turn_id": r["turn_id"],
                 "items": r.get("items"), "parent_tool_use_id": r.get("parent_tool_use_id"),
                 "always_allowed": r.get("always_allowed", True),
                 "suggestions": r.get("suggestions") or [],
                 "engine_managed": bool(r.get("raw_decision")),
                 # Who may resolve it (the identity it was raised under). Server-side
                 # only: the WS re-emit copies selected keys, never this one.
                 "owner": r["identity"]}
                for pid, r in self._pending.items() if r["session_id"] == session_id]

    def rethread_identity(self, session_id, old, new) -> int:
        """Hand *session_id*'s pending prompts registered under *old* to *new*.

        Same contract as QuestionRegistry.rethread_identity (see its docstring for
        the failure it closes): ``request`` snapshots the driver's identity and
        ``resolve`` gates on it, so a prompt raised while the driver still carried
        the "system" sentinel must follow the driver when a real user attaches.
        Returns the number moved.
        """
        moved = 0
        for rec in self._pending.values():
            if rec["session_id"] == session_id and rec["identity"] == old:
                rec["identity"] = new
                moved += 1
        return moved

    def deny_all(self, session_id, reason: str | None = None) -> int:
        """Settle every pending prompt of *session_id* as denied. Returns how many
        were actually awaiting (a future already resolved counts for nothing).

        ``reason`` carries a message back to the awaiting ``can_use_tool``, which
        turns it into the model-visible ``PermissionResultDeny(message=...)`` instead
        of the bare ``"denied by user"``. It is keyed per perm_id rather than stashed
        on the driver so a call that settles SEVERAL parked prompts gives each one the
        same real reason — the production case parked three at once, and a single
        shared slot would have handed the reason to whichever coroutine woke first and
        the generic string to the other two.

        A Stop/interrupt passes no reason: it is not user guidance, and "denied by
        user" is the honest description of it.
        """
        # Drop any reason this session left behind — an awaiter cancelled before it
        # could read one would otherwise keep its entry for the life of the process.
        for pid in [p for p, (sid, _) in self._deny_reasons.items() if sid == session_id]:
            del self._deny_reasons[pid]
        if session_id in self._gate_inflight:
            self._deny_gen[session_id] = self._deny_gen.get(session_id, 0) + 1
        n = 0
        for pid in [p for p, r in self._pending.items() if r["session_id"] == session_id]:
            rec = self._pending.pop(pid)
            if not rec["future"].done():
                if reason is not None:
                    self._deny_reasons[pid] = (session_id, reason)
                rec["future"].set_result(False)
                n += 1
        return n

    def take_deny_reason(self, perm_id: str | None) -> str | None:
        """Pop the reason :meth:`deny_all` recorded for *perm_id* (None if there is
        none). Popping is what stops a reason from ever attaching to a later,
        unrelated denial of the same session."""
        if perm_id is None:
            return None
        entry = self._deny_reasons.pop(perm_id, None)
        return None if entry is None else entry[1]

def _rule_input(tool, input):
    """The request as standing rules see it: a Bash command wrapped in an engine's shell
    invocation is matched on the script it runs (``patterns.unwrap_shell``)."""
    command = input.get("command") if tool == "Bash" and isinstance(input, dict) else None
    if not isinstance(command, str):
        return input
    script = unwrap_shell(command)
    return input if script == command else {**input, "command": script}


def _resolved(value: bool):
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(value)
    return fut
