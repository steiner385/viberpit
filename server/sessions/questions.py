from __future__ import annotations
import asyncio
import uuid as _uuid


class QuestionRegistry:
    """Lifecycle analogue to PermissionRegistry, but for AskUserQuestion.

    The future resolves to the tuple (selected_labels, mode), or None if the
    question was dismissed (via deny_all on disconnect/interrupt).
    """

    def __init__(self):
        # question_id -> {future, session_id, identity, turn_id, header, question, options, multi_select}
        self._pending: dict[str, dict] = {}

    def request(
        self,
        session_id: str,
        turn_id: str,
        header: str,
        question: str,
        options: list[dict],
        multi_select: bool,
        identity: str,
    ) -> tuple[str, asyncio.Future]:
        """Register a pending question; return (question_id, future).

        The full question payload (turn_id, header, question, options, multi_select) is stored
        so that pending() can rebuild ev.Question events on reattach without data loss.
        The future resolves to the tuple (selected, mode) or None (dismissed).
        """
        qid = _uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[qid] = {
            "future": fut,
            "session_id": session_id,
            "identity": identity,
            "turn_id": turn_id,
            "header": header,
            "question": question,
            "options": options,
            "multi_select": multi_select,
        }
        return qid, fut

    def pending(self, session_id: str) -> list[dict]:
        """Return, in registration order, one dict per still-pending question for session_id.

        Each dict has keys: question_id, turn_id, header, question, options, multi_select.
        Already-resolved or denied questions are excluded (they have been removed from _pending).
        """
        return [
            {
                "question_id": qid,
                "turn_id": r["turn_id"],
                "header": r["header"],
                "question": r["question"],
                "options": r["options"],
                "multi_select": r["multi_select"],
            }
            for qid, r in self._pending.items()
            if r["session_id"] == session_id
        ]

    def resolve(self, question_id: str, selected: list[str], identity: str,
                mode: str = "option", session_id: str | None = None) -> bool:
        """Resolve a pending question with the user's selection and answer mode.

        ``mode`` is one of "option" (picked a predefined option), "freeform"
        (typed their own answer to this question), or "chat" (chose to discuss
        rather than pick). The future resolves to the tuple ``(selected, mode)``;
        ``deny_all`` instead resolves to ``None`` (dismissed sentinel).

        When ``session_id`` is given and the exact ``question_id`` has no live
        waiter, the answer is reconciled against the session's pending question(s)
        — see ``_reconcile``. This is what unsticks a reattached session: a question
        card rebuilt from the saved transcript carries a replay-derived id
        (``{tool_use_id}-{index}``) that never matches the live registry's random
        uuid id, so an exact lookup misses and the answer would otherwise be dropped,
        leaving the turn blocked forever.

        Returns True if resolved, False if unknown id, identity mismatch, or already done.
        """
        rec = self._pending.get(question_id)
        if rec is None and session_id is not None:
            question_id, rec = self._reconcile(question_id, session_id)
        if rec is None or rec["identity"] != identity:
            return False
        del self._pending[question_id]
        if not rec["future"].done():
            rec["future"].set_result((selected, mode))
            return True
        return False

    def _reconcile(self, question_id: str, session_id: str) -> tuple[str, dict | None]:
        """Map a replay-derived question id onto a live pending question.

        The transcript card and the live registry use different id schemes
        (``{tool_use_id}-{index}`` vs. a random uuid), so an exact lookup misses
        on reattach. Match by the trailing ``-<index>`` when present (handles a
        multi-question turn), else fall back to the sole pending question. Returns
        ``(live_question_id, record)``, or ``(question_id, None)`` when it can't
        disambiguate (e.g. several pending questions and no usable index).
        """
        pend = [(qid, r) for qid, r in self._pending.items()
                if r["session_id"] == session_id]
        if not pend:
            return question_id, None
        tail = question_id.rsplit("-", 1)[-1] if "-" in question_id else ""
        if tail.isdigit():
            idx = int(tail)
            if 0 <= idx < len(pend):
                return pend[idx]
            return question_id, None
        if len(pend) == 1:
            return pend[0]
        return question_id, None

    def deny_all(self, session_id: str) -> None:
        """Resolve all pending questions for session_id with None (dismissed sentinel).

        Must be called on disconnect/interrupt/close so the blocked can_use_tool
        callback is never left awaiting forever.
        """
        for qid in [q for q, r in self._pending.items() if r["session_id"] == session_id]:
            rec = self._pending.pop(qid)
            if not rec["future"].done():
                rec["future"].set_result(None)
