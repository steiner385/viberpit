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
    (``sdk_factory.real_sdk_factory``) and live mode switches
    (``SessionDriver.set_permission_mode``) route through here so the two can
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
        if mode in ("full-auto", "bypassPermissions"):
            # Full auto / bypass-permissions — incl. shell: approve everything
            # without a prompt. ``bypassPermissions`` mirrors the SDK's own name
            # (the SDK bypasses its checks too), but the registry stays the
            # authority so the outcome is identical whether or not the SDK routes
            # the call through can_use_tool.
            return None, _resolved(True)
        # Grant-cache bypass ("Always allow this exact command/file") is honoured in all
        # modes EXCEPT "interactive" ("Ask each time").  A user who chose interactive
        # explicitly wants approval every time, so previous "always" grants are ignored.
        if mode != "interactive" and allowlist and self.is_granted(session_id, tool, input, now):
            return None, _resolved(True)
        # Auto-allow the agent's own auto-memory writes (managed-account memory dir).
        # Honoured in EVERY mode, including "interactive": these writes are sandboxed
        # to cfg_root() (see _is_account_memory_path) and can never reach a real repo,
        # so they are pure bookkeeping noise the user should never have to approve.
        # Unlike the grant-cache bypass above, "ask each time" does not apply here —
        # there is no user-facing action to ask about.
        if tool in _FILE_WRITE_TOOLS and _is_account_memory_path(
                input.get("file_path") or input.get("notebook_path")):
            return None, _resolved(True)
        # Auto-allow MCP tools (`mcp__<server>__<tool>`) in EVERY mode, including
        # "interactive".  The operator opted into trusting MCP unconditionally, so a
        # configured MCP server's tools are never gated here.  (The binary's separate
        # project-`.mcp.json` server-trust gate is suppressed in sdk_factory via
        # enableAllProjectMcpServers — this only governs ccswitch's own per-tool card.)
        if tool.startswith("mcp__"):
            return None, _resolved(True)
        if mode == "auto" and tool not in denylist:
            # Auto-approve safe tools: approve anything not on the dangerous denylist.
            return None, _resolved(True)
        if mode == "allowlist" and tool in self.READ_ONLY:
            # Allowlist read-only, prompt the rest: only strictly read-only tools are
            # auto-approved; write-capable tools (TodoWrite, etc.) must prompt even if
            # they are not on the DANGEROUS denylist.
            return None, _resolved(True)
        if mode == "acceptEdits" and tool in _FILE_WRITE_TOOLS:
            # Auto-accept file edits (Edit/Write/MultiEdit/NotebookEdit); every other
            # tool still prompts. Mirrors the SDK's acceptEdits semantics and is the
            # default for unattended automation sessions (issue #750): an agent editing
            # its own worktree proceeds without a human, while shell/network calls — a
            # materially larger blast radius — remain gated.
            return None, _resolved(True)
        pid = _uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[pid] = {"future": fut, "identity": identity, "session_id": session_id,
                              "tool": tool, "input": input, "turn_id": turn_id}
        return pid, fut

    def resolve(self, perm_id, decision, identity) -> bool:
        rec = self._pending.get(perm_id)
        if rec is None or rec["identity"] != identity:
            return False
        del self._pending[perm_id]
        if decision == "always":
            fp = self.fingerprint(rec["tool"], rec["input"])
            self._grants[(rec["session_id"], fp)] = time.time() + self.GRANT_TTL
            decision = "allow"
        if not rec["future"].done():
            rec["future"].set_result(decision == "allow")
        return True

    def pending(self, session_id) -> list[dict]:
        return [{"permId": pid, "tool": r["tool"], "input": r["input"], "turn_id": r["turn_id"]}
                for pid, r in self._pending.items() if r["session_id"] == session_id]

    def deny_all(self, session_id):
        for pid in [p for p, r in self._pending.items() if r["session_id"] == session_id]:
            rec = self._pending.pop(pid)
            if not rec["future"].done():
                rec["future"].set_result(False)

def _resolved(value: bool):
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(value)
    return fut
