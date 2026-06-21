from __future__ import annotations
import asyncio
import uuid as _uuid


class QuestionRegistry:
    """Lifecycle analogue to PermissionRegistry, but for AskUserQuestion.

    The future resolves to the user's chosen list[str] of labels, or None
    if the question was dismissed (via deny_all on disconnect/interrupt).
    """

    def __init__(self):
        # question_id -> {future, session_id, identity}
        self._pending: dict[str, dict] = {}

    def request(self, session_id: str, turn_id: str, identity: str) -> tuple[str, asyncio.Future]:
        """Register a pending question; return (question_id, future).

        The future resolves to list[str] (user's selected labels) or None (dismissed).
        """
        qid = _uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[qid] = {"future": fut, "session_id": session_id, "identity": identity}
        return qid, fut

    def resolve(self, question_id: str, selected: list[str], identity: str) -> bool:
        """Resolve a pending question with the user's selection.

        Returns True if resolved, False if unknown id, identity mismatch, or already done.
        """
        rec = self._pending.get(question_id)
        if rec is None or rec["identity"] != identity:
            return False
        del self._pending[question_id]
        if not rec["future"].done():
            rec["future"].set_result(selected)
            return True
        return False

    def deny_all(self, session_id: str) -> None:
        """Resolve all pending questions for session_id with None (dismissed sentinel).

        Must be called on disconnect/interrupt/close so the blocked can_use_tool
        callback is never left awaiting forever.
        """
        for qid in [q for q, r in self._pending.items() if r["session_id"] == session_id]:
            rec = self._pending.pop(qid)
            if not rec["future"].done():
                rec["future"].set_result(None)
