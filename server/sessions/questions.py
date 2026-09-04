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

    def resolve_all(self, session_id: str, selected: list[str], mode: str = "chat") -> int:
        """Resolve EVERY pending question of *session_id* with the same answer.

        The driver's ``send_prompt`` uses this when a plain user message arrives while
        the turn is parked on AskUserQuestion: the message is the user's reply to the
        call as a whole, so a multi-question call is answered as a group. No identity
        check — the caller is the driver that owns the session (a prompt already
        carries no identity gate; answering the pending question with it widens
        nothing). Returns the number of questions resolved (0 when none pending).
        """
        pend = [qid for qid, r in self._pending.items() if r["session_id"] == session_id]
        n = 0
        for qid in pend:
            rec = self._pending.pop(qid)
            if not rec["future"].done():
                rec["future"].set_result((selected, mode))
                n += 1
        return n

    def _reconcile(self, question_id: str, session_id: str) -> tuple[str, dict | None]:
        """Map an unrecognized question id onto the session's pending question.

        A card rebuilt from the transcript carries a replay-derived id
        (``{tool_use_id}-{index}``) that never matches the live registry's random
        uuid, so an exact lookup misses on reattach. Reconcile ONLY when exactly one
        question is pending for the session — then the target is unambiguous.

        With several pending (a multi-question turn) we deliberately refuse to guess.
        Matching by position is unsafe: the pending list shrinks as questions are
        answered, so a stale or duplicate answer could resolve the WRONG remaining
        question. It is also unnecessary — the answerable cards in a multi-question
        turn carry exact live ids, so they hit the exact lookup above and never reach
        reconciliation. Returns ``(live_question_id, record)`` or
        ``(question_id, None)`` when it won't disambiguate.
        """
        pend = [(qid, r) for qid, r in self._pending.items()
                if r["session_id"] == session_id]
        if len(pend) == 1:
            return pend[0]
        return question_id, None

    def rethread_identity(self, session_id: str, old: str, new: str) -> int:
        """Hand *session_id*'s pending questions registered under *old* to *new*.

        ``request`` snapshots the driver's identity at ask time, and ``resolve`` gates
        on that snapshot. A driver built by an internal path (rehydrate, deploy
        restore, dispatch) carries the "system" sentinel, so a question the CLI asks
        BEFORE a real user attaches is owned by "system" — and when that user then
        attaches and answers, the gate rejects them even though ``pending`` still
        lists the question ("That question is no longer pending" over a card that
        is very much pending). The orchestrator upgrades ``driver.identity`` on
        attach; this carries the already-registered asks along so the two never
        disagree. Only records under *old* move — a question a different real user
        owns is never handed over. Returns the number moved.
        """
        moved = 0
        for rec in self._pending.values():
            if rec["session_id"] == session_id and rec["identity"] == old:
                rec["identity"] = new
                moved += 1
        return moved

    def deny_all(self, session_id: str) -> None:
        """Resolve all pending questions for session_id with None (dismissed sentinel).

        Must be called on disconnect/interrupt/close so the blocked can_use_tool
        callback is never left awaiting forever.
        """
        for qid in [q for q, r in self._pending.items() if r["session_id"] == session_id]:
            rec = self._pending.pop(qid)
            if not rec["future"].done():
                rec["future"].set_result(None)
