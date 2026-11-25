"""claude.ai's events log for one Remote Control session -> ViberPit wire events.

The log holds the CLI's own stream-json (``system``, ``user``, ``assistant``,
``rate_limit_event``, ``result``, plus ``control_request`` / ``control_response`` /
``control_cancel_request``), consolidated: no partial-message stream events. Everything
but the control frames goes through the SAME parse (``claude_driver.parse_cli_message``)
and the SAME ``Translator`` a live ViberPit session uses, so the web chat renders it
unchanged. The translator runs with ``consolidated_text`` (whole text blocks, since there
are no deltas) and ``remote_bridge_open`` (every typed prompt renders as a bubble: no
local driver emitted one).

A ``can_use_tool`` control request becomes a permission card (``permId`` = the CLI's
``request_id``) or, for ``AskUserQuestion``, one question card per question
(``questionId`` = ``<request_id>:<index>``). The ask stays pending until the log shows it
settled — a ``control_response`` or ``control_cancel_request`` for it, a tool result for
its tool use, or the turn's ``result`` — and is then resolved on the stream. Who answered
is ViberPit when :meth:`RemoteEventTranslator.note_answer` recorded it, else the Claude
app / the CLI (``via`` ``claude_app``, outcome unknown).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from server.sessions import answered_elsewhere
from server.sessions import events as ev
from server.sessions.translator import Translator

log = logging.getLogger(__name__)

ASK_USER_QUESTION = "AskUserQuestion"
EXIT_PLAN_MODE = "ExitPlanMode"

#: Translator events a live driver consumes internally and never forwards to the UI.
_INTERNAL = (ev.RateLimit, ev.ApiRetry, ev.HandBackRequested, ev.UserPromptUuid)

# Status states, as the driver reports them (``ev.Status.state``).
STATE_IDLE = "idle"
STATE_WORKING = "in_progress"
STATE_WAITING = "waiting_input"


def question_id(request_id: str, index: int) -> str:
    return f"{request_id}:{index}"


def split_question_id(qid: str) -> tuple[str, int] | None:
    """``(request_id, index)`` for a question id, or None when *qid* is not one."""
    rid, sep, idx = qid.rpartition(":")
    if not sep or not rid or not idx.isdigit():
        return None
    return rid, int(idx)


@dataclass
class PendingAsk:
    """A ``can_use_tool`` request the log has not shown settled yet."""
    request_id: str
    turn_id: str
    tool_name: str
    input: dict
    tool_use_id: str | None
    questions: list[dict] = field(default_factory=list)   # AskUserQuestion only
    # Question index -> (selected labels, mode), collected until every question is answered.
    answers: dict[int, tuple[list[str], str]] = field(default_factory=dict)

    @property
    def is_question(self) -> bool:
        return self.tool_name == ASK_USER_QUESTION


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _normalize_option(opt: Any) -> dict:
    return opt if isinstance(opt, dict) else {"label": str(opt)}


class RemoteEventTranslator:
    """Stateful: one per watched session, fed every log event in order."""

    def __init__(self) -> None:
        self._tr = Translator(consolidated_text=True)
        self._tr.remote_bridge_open = True
        self.pending: dict[str, PendingAsk] = {}
        self._turn_id = "turn"
        self._turn_open = False
        self._state: str | None = None
        # request_id -> what ViberPit answered: ("permission", allowed) or
        # ("question", {index: labels}).
        self._ours: dict[str, tuple[str, Any]] = {}

    # -- public --------------------------------------------------------------------

    def feed(self, raw: Any) -> list[dict]:
        """The wire events one log event stands for (possibly none). Never raises: a
        malformed event is logged at DEBUG and skipped."""
        if not isinstance(raw, dict):
            return []
        try:
            events = self._feed(raw)
        except Exception as exc:  # noqa: BLE001 — one odd event must not stop the watch
            log.debug("remote sessions: skipped an event of type %r (%s)",
                      raw.get("type"), type(exc).__name__)
            events = []
        out = [e.to_wire() for e in events if not isinstance(e, _INTERNAL)]
        status = self._status_event()
        if status is not None:
            out.append(status.to_wire())
        return out

    def note_answer(self, request_id: str, kind: str, value: Any) -> None:
        """Record that ViberPit answered *request_id*, so its resolution reports the
        answer instead of "answered elsewhere"."""
        self._ours[request_id] = (kind, value)

    # -- internals -----------------------------------------------------------------

    def _status_event(self) -> ev.Status | None:
        state = (STATE_WAITING if self.pending
                 else STATE_WORKING if self._turn_open else STATE_IDLE)
        if state == self._state:
            return None
        self._state = state
        return ev.Status(state)

    def _feed(self, raw: dict) -> list:
        kind = raw.get("type")
        if kind == "control_request":
            return self._control_request(raw)
        if kind in ("control_response", "control_cancel_request"):
            rid = raw.get("request_id")
            if kind == "control_response":
                resp = _dict(raw.get("response"))
                rid = resp.get("request_id")
                inner = _dict(resp.get("response"))
                behavior = inner.get("behavior")
                allowed = True if behavior == "allow" else False if behavior == "deny" else None
            else:
                allowed = None
            ask = self.pending.pop(rid, None) if isinstance(rid, str) else None
            return self._resolved(ask, allowed) if ask is not None else []
        from server.sessions.claude_driver import parse_cli_message
        msg = parse_cli_message(raw)
        if msg is None:
            return []
        out: list = []
        if kind == "result":
            # A turn cannot end on a pending ask: whatever is still open was settled
            # somewhere the log does not show.
            for ask in list(self.pending.values()):
                out.extend(self._resolved(ask, None))
            self.pending.clear()
            self._turn_open = False
        elif kind in ("assistant", "user"):
            self._turn_open = True
            if kind == "user":
                out.extend(self._settle_by_tool_result(raw))
        translated = self._tr.feed(msg)
        for e in translated:
            if isinstance(e, ev.TurnStart) and e.parent_tool_use_id is None:
                self._turn_id = e.turn_id
        return out + translated

    def _settle_by_tool_result(self, raw: dict) -> list:
        message = _dict(raw.get("message"))
        content = message.get("content")
        if not isinstance(content, list):
            return []
        done = {b.get("tool_use_id") for b in content
                if isinstance(b, dict) and b.get("type") == "tool_result"}
        out: list = []
        for rid, ask in list(self.pending.items()):
            if ask.tool_use_id is not None and ask.tool_use_id in done:
                del self.pending[rid]
                out.extend(self._resolved(ask, None))
        return out

    def _control_request(self, raw: dict) -> list:
        rid = raw.get("request_id")
        req = _dict(raw.get("request"))
        if not isinstance(rid, str) or not rid or req.get("subtype") != "can_use_tool":
            return []
        tool = req.get("tool_name")
        tool_input = _dict(req.get("input"))
        if not isinstance(tool, str) or not tool:
            return []
        tool_use_id = req.get("tool_use_id") if isinstance(req.get("tool_use_id"), str) else None
        ask = PendingAsk(request_id=rid, turn_id=self._turn_id, tool_name=tool,
                         input=tool_input, tool_use_id=tool_use_id)
        if ask.is_question:
            raw_qs = tool_input.get("questions")
            ask.questions = [q for q in raw_qs if isinstance(q, dict)] \
                if isinstance(raw_qs, list) else []
            if not ask.questions:
                return []
        self.pending[rid] = ask
        self._turn_open = True
        if ask.is_question:
            return [ev.Question(
                question_id=question_id(rid, i), turn_id=ask.turn_id,
                header=str(q.get("header") or ""), question=str(q.get("question") or ""),
                options=[_normalize_option(o) for o in (q.get("options") or [])
                         ] if isinstance(q.get("options"), list) else [],
                multi_select=bool(q.get("multiSelect", False)))
                for i, q in enumerate(ask.questions)]
        # No "always allow" and no session grant: a standing rule would be ViberPit's,
        # and this CLI never consults ViberPit's rules.
        return [ev.PermissionRequest(
            rid, ask.turn_id, tool, ev.tool_kind(tool), tool_input,
            plan=(tool == EXIT_PLAN_MODE), always_allowed=False,
            session_grant_effective=False)]

    def _resolved(self, ask: PendingAsk, allowed: bool | None) -> list:
        ours = self._ours.pop(ask.request_id, None)
        if ask.is_question:
            picked: dict = ours[1] if ours is not None and ours[0] == "question" else {}
            return [ev.QuestionResolved(
                question_id(ask.request_id, i), ask.turn_id,
                list(picked[i][0]) if i in picked else None,
                mode=picked[i][1] if i in picked else "option",
                via=None if ours is not None else answered_elsewhere.VIA_CLAUDE_APP)
                for i in range(len(ask.questions))]
        if ours is not None and ours[0] == "permission":
            return [ev.PermissionResolved(ask.request_id, ask.turn_id, bool(ours[1]))]
        return [ev.PermissionResolved(ask.request_id, ask.turn_id, allowed,
                                      via=answered_elsewhere.VIA_CLAUDE_APP)]
