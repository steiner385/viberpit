from __future__ import annotations
import asyncio, os, time, uuid as _uuid
from pathlib import Path

from ccswitch import paths as _paths

# File-writing tools whose target is a single path argument.
_FILE_WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# ccswitch session modes whose names mirror the Claude Agent SDK's own
# ``permission_mode`` vocabulary. For these the SDK's permission engine is the
# front-line authority (it can auto-accept edits / bypass checks before the
# ``can_use_tool`` callback even fires); every other ccswitch mode
# (interactive/auto/allowlist/full-auto) runs the SDK on ``"default"`` and is
# gated entirely by ``PermissionRegistry.request`` below.
_SDK_PASSTHROUGH_MODES = frozenset({"plan", "acceptEdits", "bypassPermissions"})


def to_sdk_permission_mode(mode: str) -> str:
    """Map a ccswitch session mode to the Claude Agent SDK ``permission_mode``.

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
        from ccswitch import ledger as _ledger
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
    """
    DANGEROUS = frozenset({"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch"})
    # Tools that only read filesystem/index state and produce no side-effects.
    # Used by the "allowlist" mode ("Allowlist read-only, prompt the rest") to decide
    # which tools to auto-approve; everything else — including TodoWrite — must prompt.
    READ_ONLY = frozenset({"Read", "Grep", "Glob", "LS"})
    GRANT_TTL = 3600  # seconds

    def __init__(self):
        self._pending: dict[str, dict] = {}              # perm_id -> {future, identity, session_id, tool, input, turn_id}
        self._grants: dict[tuple[str, str], float] = {}  # (session_id, fingerprint) -> expires_at

    @staticmethod
    def fingerprint(tool: str, input: dict) -> str:
        if tool == "Bash":
            cmd = " ".join((input.get("command") or "").split())   # full normalized command, not just argv[0]
            return f"Bash:{cmd}:{input.get('cwd', '')}"
        if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            return f"{tool}:{input.get('file_path') or input.get('notebook_path', '')}"
        if tool in ("WebFetch", "WebSearch"):
            return f"{tool}:{input.get('url') or input.get('query', '')}"
        return tool

    def is_granted(self, session_id, tool, input, now) -> bool:
        key = (session_id, self.fingerprint(tool, input))
        exp = self._grants.get(key)
        if exp is None:
            return False
        if exp <= now:
            del self._grants[key]
            return False
        return True

    async def request(self, session_id, turn_id, tool, input, *, mode, denylist, allowlist, identity):
        now = time.time()
        # ccswitch's own MCP server (`mcp__ccswitch__*`) is the app talking to
        # itself, not a third-party integration trusted unconditionally like the
        # generic `mcp__` branch below — spec §4.4. Its cards are gated on the
        # tool's ANNOTATION (read/mutation/destructive), not on session mode, so
        # this branch sits ABOVE both the full-auto/bypassPermissions shortcut and
        # the generic mcp__ auto-allow: a destructive ccswitch tool must prompt
        # even in full-auto. Local import avoids a module-load-order cycle with
        # server.assistant.tools (which itself does not import server.sessions,
        # but keeping this import lazy is cheap insurance).
        if tool.startswith("mcp__ccswitch__"):
            from server.assistant import tools as assistant_tools
            ann = assistant_tools.annotation_for(tool)
            if ann is not None and ann.tool_class == assistant_tools.ToolClass.READ:
                return None, _resolved(True), True
            if ann is not None and ann.tool_class == assistant_tools.ToolClass.DESTRUCTIVE:
                # Never consults the grant cache, in ANY mode — a destructive
                # ccswitch action always surfaces a card, and that card never
                # offers "Always allow" (always_allowed=False on the pending
                # record; resolve() below refuses to persist a grant for it).
                pid = _uuid.uuid4().hex
                fut = asyncio.get_running_loop().create_future()
                self._pending[pid] = {"future": fut, "identity": identity, "session_id": session_id,
                                      "tool": tool, "input": input, "turn_id": turn_id,
                                      "always_allowed": False}
                return pid, fut, False
            # MUTATION, or an unknown/not-yet-registered ccswitch tool (treated
            # conservatively as mutation-like rather than raising on a None
            # annotation — forward-compatible with tools Task 3.3 adds later).
            # Grant cache is honoured in EVERY mode, INCLUDING "interactive" —
            # unlike the generic grant-cache check below, which deliberately
            # skips "interactive" for ordinary tools. This is deliberate here:
            # the operator granting "always allow" on a ccswitch mutation tool
            # is trusting the APP, not accepting session-mode risk.
            if self.is_granted(session_id, tool, input, now):
                return None, _resolved(True), True
            pid = _uuid.uuid4().hex
            fut = asyncio.get_running_loop().create_future()
            self._pending[pid] = {"future": fut, "identity": identity, "session_id": session_id,
                                  "tool": tool, "input": input, "turn_id": turn_id,
                                  "always_allowed": True}
            return pid, fut, True
        if mode in ("full-auto", "bypassPermissions"):
            # Full auto / bypass-permissions — incl. shell: approve everything
            # without a prompt. ``bypassPermissions`` mirrors the SDK's own name
            # (the SDK bypasses its checks too), but the registry stays the
            # authority so the outcome is identical whether or not the SDK routes
            # the call through can_use_tool.
            return None, _resolved(True), True
        # Grant-cache bypass ("Always allow this exact command/file") is honoured in all
        # modes EXCEPT "interactive" ("Ask each time").  A user who chose interactive
        # explicitly wants approval every time, so previous "always" grants are ignored.
        if mode != "interactive" and allowlist and self.is_granted(session_id, tool, input, now):
            return None, _resolved(True), True
        # Auto-allow the agent's own auto-memory writes (managed-account memory dir).
        # Honoured in EVERY mode, including "interactive": these writes are sandboxed
        # to cfg_root() (see _is_account_memory_path) and can never reach a real repo,
        # so they are pure bookkeeping noise the user should never have to approve.
        # Unlike the grant-cache bypass above, "ask each time" does not apply here —
        # there is no user-facing action to ask about.
        if tool in _FILE_WRITE_TOOLS and _is_account_memory_path(
                input.get("file_path") or input.get("notebook_path")):
            return None, _resolved(True), True
        # Auto-allow MCP tools (`mcp__<server>__<tool>`) in EVERY mode, including
        # "interactive".  The operator opted into trusting MCP unconditionally, so a
        # configured MCP server's tools are never gated here.  (The binary's separate
        # project-`.mcp.json` server-trust gate is suppressed in sdk_factory via
        # enableAllProjectMcpServers — this only governs ccswitch's own per-tool card.)
        # ccswitch's own `mcp__ccswitch__*` tools are intercepted above and never
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
        pid = _uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[pid] = {"future": fut, "identity": identity, "session_id": session_id,
                              "tool": tool, "input": input, "turn_id": turn_id,
                              "always_allowed": True}
        return pid, fut, True

    async def request_external(self, session_id, turn_id, tool, input, *, identity,
                               always_allowed: bool = True):
        """Register a prompt an ENGINE already decided needs a human, and return
        ``(perm_id, future)`` where the future settles to the raw ccswitch
        decision string (``"allow"`` / ``"deny"`` / ``"always"``) — or ``False``
        if it was dismissed by :meth:`deny_all`.

        Two differences from :meth:`request`, both forced by who is deciding:

        * **No auto-approval, in any mode.** ``request`` re-derives "does this
          need a human?" from the ccswitch mode because the Claude SDK asks about
          every tool. An external engine asks only when *its own* policy already
          concluded a human is required, and that policy was itself derived from
          the same ccswitch mode (``server/codex/permissions.py``). Re-applying
          the mode here would auto-answer a question the engine only raises in
          the modes where ccswitch wanted the prompt — and it would answer it
          ``allow``.
        * **The future carries the decision, not a boolean, and "always" never
          writes a local grant.** ``self._grants`` is ccswitch's own cache, and
          it is never consulted for an engine that enforces approvals itself, so
          an entry there would be a second, invisible policy that no longer
          matches what the engine was told. The caller maps ``"always"`` onto the
          engine's own SESSION-SCOPED accept instead — never a persistent global
          exception (plan, Task 4.2).

        Everything else is deliberately shared with ordinary prompts: the same
        ``perm_id`` namespace, the same identity ownership check in
        :meth:`resolve`, the same :meth:`pending` / :meth:`rethread_identity` /
        :meth:`deny_all` semantics, and therefore the same WebSocket frames, REST
        route and UI card. There is no parallel approval registry.
        """
        pid = _uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[pid] = {"future": fut, "identity": identity, "session_id": session_id,
                              "tool": tool, "input": input, "turn_id": turn_id,
                              "always_allowed": always_allowed, "raw_decision": True}
        return pid, fut

    def resolve(self, perm_id, decision, identity) -> bool:
        rec = self._pending.get(perm_id)
        if rec is None or rec["identity"] != identity:
            return False
        del self._pending[perm_id]
        raw = rec.get("raw_decision", False)
        if decision == "always":
            if rec.get("always_allowed", True) and not raw:
                fp = self.fingerprint(rec["tool"], rec["input"])
                self._grants[(rec["session_id"], fp)] = time.time() + self.GRANT_TTL
            # else: a destructive ccswitch tool, or an engine-enforced prompt whose
            # "always" is honoured natively — allow this ONE call but never persist a
            # ccswitch grant, so the next request() for the same tool asks again.
            if not raw:
                decision = "allow"
        if not rec["future"].done():
            rec["future"].set_result(decision if raw else decision == "allow")
        return True

    def pending(self, session_id) -> list[dict]:
        return [{"permId": pid, "tool": r["tool"], "input": r["input"], "turn_id": r["turn_id"]}
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

    def deny_all(self, session_id):
        for pid in [p for p, r in self._pending.items() if r["session_id"] == session_id]:
            rec = self._pending.pop(pid)
            if not rec["future"].done():
                rec["future"].set_result(False)

def _resolved(value: bool):
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(value)
    return fut
