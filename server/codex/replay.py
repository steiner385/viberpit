"""`EngineReplay` for a Codex thread — history read back over `thread/read`.

Codex sessions have no transcript file. Their history lives inside the
app-server, and the plan forbids reading Codex private state (the rollout SQLite,
the thread JSONL) to reconstruct it: those are internal formats with no
compatibility promise, and parsing them would make ccswitch break on a Codex
patch release. So history comes back the supported way — one `thread/read` with
`includeTurns=true` — and is translated by the SAME
:class:`~server.codex.translator.CodexEventTranslator` the live stream uses.

That shared translator is the point. If replay had its own item→event mapping,
the transcript a user sees after a reload could differ from the one they watched
live, and the difference would appear only for item types nobody wrote a test
for twice.

Cursor semantics are inherited from `server/sessions/replay.py`, not reinvented:
`before` and `limit` are ordinals in a turn space, `earliest_turn` is the ordinal
of the window's first turn, and `total_turns` is the total. One Codex turn is one
ordinal — which is actually cleaner than the Claude adapter, where a "turn" is
one assistant JSONL entry and a tool-heavy logical turn spans several.
"""
from __future__ import annotations

from typing import Protocol

from server.codex import schema
from server.codex.translator import CodexEventTranslator
from server.sessions import events as ev
from server.sessions.replay import ReplayWindow


class ThreadReader(Protocol):
    """The one app-server call this adapter needs.

    Narrower than `CodexAppServerClient` on purpose: replay is a pure read, so
    depending on the whole client would let a future edit here reach `turn/start`
    — and would force every test to stand up a full JSONL transport to exercise
    windowing arithmetic.
    """

    async def read_thread(
        self, params: schema.ThreadReadParams
    ) -> schema.ThreadReadResponse: ...


class CodexThreadReplay:
    """`EngineReplay` over one Codex thread's `thread/read` history."""

    __slots__ = ("_client", "_thread_id", "_model")

    def __init__(
        self,
        client: ThreadReader,
        thread_id: str,
        *,
        model: str | None = None,
    ) -> None:
        self._client = client
        self._thread_id = thread_id
        self._model = model

    async def _read(self) -> schema.Thread:
        response = await self._client.read_thread(
            schema.ThreadReadParams(thread_id=self._thread_id, include_turns=True)
        )
        return response.thread

    def _events_for(self, turn: schema.Turn, ordinal: int) -> list:
        """One turn's events, in the order the live stream would have produced them.

        The leading `userMessage` items come out BEFORE the `TurnStart`, mirroring
        a Claude transcript where the user's entry precedes the assistant's. The
        reducer nests a turn's content under the `TurnStart` that opens it, so
        emitting the prompt inside the turn would render it as assistant output.
        """
        translator = CodexEventTranslator(
            thread_id=self._thread_id, model=self._model
        )
        prologue: list = []
        body: list = []
        opened = False
        for item in turn.items:
            translated = translator.translate_item(
                item, turn_id=turn.id, completed=True
            )
            if not opened and isinstance(item, schema.UserMessageItem):
                prologue.extend(translated)
                continue
            if not opened:
                opened = True
                # Stamped with its ordinal in the cursor space, exactly as the
                # Claude adapter's `_window` stamps its own — the client uses it
                # to verify that page merges preserve turn order.
                body.append(ev.TurnStart(turn.id, turn_index=ordinal))
            body.extend(translated)
        if not opened:
            body.append(ev.TurnStart(turn.id, turn_index=ordinal))
        body.append(
            ev.TurnEnd(turn.id, outcome=_outcome_of(turn.status))
        )
        return prologue + body

    async def window(
        self,
        *,
        before: int | None = None,
        limit: int | None = 20,
        drop_incomplete_turn: bool = False,
        turn_started_at: float | None = None,
    ) -> ReplayWindow:
        """Return up to *limit* turns ending before ordinal *before*.

        ``turn_started_at`` is accepted for interface parity and deliberately
        unused. It exists because Claude's transcript is written to disk with a
        lag, so the reader has to guess which trailing user entry belongs to the
        turn that is actually running. Codex reports `TurnStatus` authoritatively,
        so ``drop_incomplete_turn`` can simply drop an `inProgress` turn — no
        heuristic, and no way for it to amputate a completed one.
        """
        thread = await self._read()
        turns = thread.turns
        if drop_incomplete_turn:
            while turns and turns[-1].status is schema.TurnStatus.IN_PROGRESS:
                turns = turns[:-1]
        # Leads every window, exactly as the Claude adapter prepends its
        # transcript's meta entry. The live `SessionMeta` is emitted once at
        # thread start and does not survive in the driver's turn buffer, so
        # without this a client attaching to an existing session would never
        # learn which model it is talking to.
        meta = self._session_meta(thread)
        total = len(turns)
        if total == 0:
            return ReplayWindow(
                events=[] if meta is None else [meta],
                earliest_turn=0,
                total_turns=0,
            )
        end = total if before is None else max(0, min(before, total))
        start = 0 if limit is None else max(0, end - limit)
        out: list = [] if meta is None else [meta]
        for ordinal in range(start, end):
            out.extend(self._events_for(turns[ordinal], ordinal))
        return ReplayWindow(events=out, earliest_turn=start, total_turns=total)

    def _session_meta(self, thread: schema.Thread) -> ev.SessionMeta | None:
        model = thread.model or self._model
        if model is None:
            return None
        return ev.SessionMeta(model=model, session_id=thread.id)

    async def events(self, *, max_turns: int | None = 20) -> list:
        window = await self.window(before=None, limit=max_turns)
        return window.events


def _outcome_of(status: schema.TurnStatus) -> str:
    """Map a completed turn's native status onto the normalized outcome.

    An `inProgress` turn reaching here means the caller asked to KEEP the live
    turn (`drop_incomplete_turn=False`), so it is closed as interrupted rather
    than successful: replay cannot know how it will end, and "success" is the one
    answer that must never be guessed.
    """
    if status is schema.TurnStatus.COMPLETED:
        return "success"
    if status is schema.TurnStatus.FAILED:
        return "error"
    return "interrupted"
