from __future__ import annotations
import asyncio, os, time, uuid as _uuid
from pathlib import Path

from ccswitch import paths as _paths

# File-writing tools whose target is a single path argument.
_FILE_WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})


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
        if mode == "full-auto":
            # Full auto — incl. shell: approve everything without a prompt.
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
        if mode == "auto" and tool not in denylist:
            # Auto-approve safe tools: approve anything not on the dangerous denylist.
            return None, _resolved(True)
        if mode == "allowlist" and tool in self.READ_ONLY:
            # Allowlist read-only, prompt the rest: only strictly read-only tools are
            # auto-approved; write-capable tools (TodoWrite, etc.) must prompt even if
            # they are not on the DANGEROUS denylist.
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
