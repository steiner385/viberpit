from __future__ import annotations
import asyncio, time, uuid as _uuid

class PermissionRegistry:
    DANGEROUS = frozenset({"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch"})
    GRANT_TTL = 3600  # seconds

    def __init__(self):
        self._pending: dict[str, dict] = {}              # perm_id -> {future, identity, session_id, tool, input, turn_id}
        self._grants: dict[tuple[str, str], float] = {}  # (session_id, fingerprint) -> expires_at

    @staticmethod
    def fingerprint(tool: str, input: dict) -> str:
        if tool == "Bash":
            cmd = (input.get("command") or "").strip().split()
            return f"Bash:{cmd[0] if cmd else ''}:{input.get('cwd', '')}"
        if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            return f"{tool}:{input.get('file_path') or input.get('notebook_path', '')}"
        return tool

    def is_granted(self, session_id, tool, input, now) -> bool:
        exp = self._grants.get((session_id, self.fingerprint(tool, input)))
        return exp is not None and exp > now

    async def request(self, session_id, turn_id, tool, input, *, mode, denylist, allowlist, identity):
        now = time.time()
        if mode == "full-auto":
            return None, _resolved(True)
        if allowlist and self.is_granted(session_id, tool, input, now):
            return None, _resolved(True)
        if mode in ("auto", "allowlist") and tool not in denylist:
            return None, _resolved(True)
        pid = _uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
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
    fut = asyncio.get_event_loop().create_future()
    fut.set_result(value)
    return fut
