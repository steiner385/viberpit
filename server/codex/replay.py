"""`EngineReplay` for Codex — native history plus durable live-turn gap recovery.

Native history lives inside the
app-server, and the plan forbids reading Codex private state (the rollout SQLite,
the thread JSONL) to reconstruct it: those are internal formats with no
compatibility promise, and parsing them would make ViberPit break on a Codex
patch release. So history comes back the supported way — one `thread/read` with
`includeTurns=true` — and is translated by the SAME
:class:`~server.codex.translator.CodexEventTranslator` the live stream uses.

That shared translator is the point. If replay had its own item→event mapping,
the transcript a user sees after a reload could differ from the one they watched
live, and the difference would appear only for item types nobody wrote a test
for twice.

Completed live turns also have ViberPit-owned sidecar snapshots. When native
history omits a resumed turn, replay inserts its saved normalized events rather
than silently rolling back the conversation. Native turns win once they catch up.
Each healed gap is logged and counted (#2933), so native history lagging the
live stream is visible rather than silently papered over.

Cursor semantics are inherited from `server/sessions/replay.py`, not reinvented:
`before` and `limit` are ordinals in a turn space, `earliest_turn` is the ordinal
of the window's first turn, and `total_turns` is the total. One Codex turn is one
ordinal — which is actually cleaner than the Claude adapter, where a "turn" is
one assistant JSONL entry and a tool-heavy logical turn spans several.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path
from typing import Protocol

from server.codex import schema
from server.codex.images import PendingToolImages, materialize_images
from server.codex.item_times import EMPTY_ITEM_TIMES, ItemTimes, load_item_times, uuid7_ms
from server.codex.translator import CodexEventTranslator
from server.codex.turn_history import RecordedTurn, TurnHistoryStore
from server.obs.context import log_event
from server.obs.logdedupe import LogThrottle
from server.obs.metrics import codex_replay_backfilled_turns_total
from server.sessions import events as ev
from server.sessions.replay import ReplayWindow

log = logging.getLogger(__name__)

# One window per (thread, gap shape); see `CodexThreadReplay._report_gap`.
_GAP_LOG = LogThrottle()


def _turn_started_ms(turn: schema.Turn) -> float | None:
    """The turn's start as epoch MILLISECONDS — the unit every `ev.*.ts` carries
    on the wire — or None when nothing states it.

    `Turn.startedAt` first: the app-server's own schema (`codex app-server
    generate-json-schema`, codex-cli 0.153.2) documents it as "Unix timestamp (in
    seconds)" and a live `thread/read` confirms it (`1789404900`, not
    `1789404900000`). Failing that, the turn id itself: turn ids are UUIDv7, whose
    embedded millisecond decodes to the same second `startedAt` reports.
    """
    if turn.started_at is not None:
        return float(turn.started_at) * 1000.0
    return uuid7_ms(turn.id)


def _item_started_ms(
    item: schema.ThreadItem, times: ItemTimes, turn_ms: float | None
) -> float | None:
    """When *item* began, by the most precise source that knows.

    1. The journalled `item/started` time (``server/codex/item_times.py``) — the
       exact millisecond the live stream saw. Only source for agent text,
       reasoning and command runs, whose ids carry no time.
    2. The item id's own UUIDv7 millisecond — `userMessage` ids are minted that
       way, so a prompt is exact even with no sidecar.
    3. The owning turn's start — one value for every block in the turn.
    4. None: an empty gutter, never a fabricated time.
    """
    started = times.started.get(item.id)
    if started is not None:
        return float(started)
    embedded = uuid7_ms(item.id)
    if embedded is not None:
        return embedded
    return turn_ms


def _stamp_for(
    event: object, item: schema.ThreadItem, times: ItemTimes, item_ms: float | None
) -> float | None:
    """A ToolResult is when the item FINISHED, so it takes the journalled
    `item/completed` time when there is one; every other block is the item's start."""
    if isinstance(event, (ev.ToolResult, PendingToolImages)):
        completed = times.completed.get(item.id)
        if completed is not None:
            return float(completed)
    return item_ms


def _restamp(event: object, ts: float | None) -> object:
    """Return *event* with its `ts` set to *ts*, or the event itself when it has no
    `ts` field (TurnStart/TurnEnd/Condensing carry none on the wire)."""
    if isinstance(event, PendingToolImages):
        return dataclasses.replace(event, result=dataclasses.replace(event.result, ts=ts))
    if not dataclasses.is_dataclass(event) or isinstance(event, type):
        return event
    if not any(f.name == "ts" for f in dataclasses.fields(event)):
        return event
    return dataclasses.replace(event, ts=ts)


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

    __slots__ = ("_client", "_thread_id", "_model", "_item_times_path", "_turn_history", "_session_id")

    def __init__(
        self,
        client: ThreadReader,
        thread_id: str,
        *,
        model: str | None = None,
        session_id: str | None = None,
        item_times_path: Path | None = None,
    ) -> None:
        self._session_id = session_id
        self._client = client
        self._thread_id = thread_id
        self._model = model
        # The per-thread sidecar of journalled item times (see item_times.py), read
        # fresh on every window because it grows while the thread runs. None means
        # "no sidecar was ever configured" — the coarser fallbacks still apply.
        self._item_times_path = item_times_path
        self._turn_history = (None if item_times_path is None
                              else TurnHistoryStore(item_times_path.parent))

    @property
    def item_times_path(self) -> Path | None:
        return self._item_times_path

    async def _read(self) -> schema.Thread:
        response = await self._client.read_thread(
            schema.ThreadReadParams(thread_id=self._thread_id, include_turns=True)
        )
        return response.thread

    def _events_for(
        self, turn: schema.Turn, ordinal: int, times: ItemTimes = EMPTY_ITEM_TIMES
    ) -> list:
        """One turn's events, in the order the live stream would have produced them.

        The leading `userMessage` items come out BEFORE the `TurnStart`, mirroring
        a Claude transcript where the user's entry precedes the assistant's. The
        reducer nests a turn's content under the `TurnStart` that opens it, so
        emitting the prompt inside the turn would render it as assistant output.

        Every block event is re-stamped with when its item happened (see
        `_item_started_ms` / `_stamp_for` for the precedence). The translator is the
        LIVE mapping and lets each event self-stamp at construction — right for a
        notification arriving now, wrong for history: without this override a
        reloaded session showed the replay time in every gutter. An item nothing
        can date replays unstamped (empty gutter), never with a fabricated time —
        the Claude adapter's missing-`timestamp` contract.
        """
        translator = CodexEventTranslator(
            thread_id=self._thread_id, model=self._model
        )
        turn_ms = _turn_started_ms(turn)
        prologue: list = []
        body: list = []
        opened = False
        for item in turn.items:
            item_ms = _item_started_ms(item, times, turn_ms)
            translated = [
                _restamp(e, _stamp_for(e, item, times, item_ms))
                for e in translator.translate_item(
                    item, turn_id=turn.id, completed=True
                )
            ]
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
        # Off the loop: the sidecar is a plain file read, and on a host whose
        # account store sits on a slow mount that read must not stall the daemon.
        times = (
            EMPTY_ITEM_TIMES
            if self._item_times_path is None
            else await asyncio.to_thread(load_item_times, self._item_times_path)
        )
        saved = ([] if self._turn_history is None else await asyncio.to_thread(
            self._turn_history.load, self._thread_id,
        ))
        turns = _merge_saved_turns(thread.turns, saved)
        self._report_gap(thread.turns, turns)
        if drop_incomplete_turn:
            while (turns and isinstance(turns[-1], schema.Turn)
                   and turns[-1].status is schema.TurnStatus.IN_PROGRESS):
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
            turn = turns[ordinal]
            if isinstance(turn, RecordedTurn):
                out.extend(dataclasses.replace(e, turn_index=ordinal)
                           if isinstance(e, ev.TurnStart) else e
                           for e in turn.events if not isinstance(e, ev.SessionMeta))
            else:
                out.extend(self._events_for(turn, ordinal, times))
        out = [await materialize_images(e, session_id=self._session_id,
                                        thread_id=self._thread_id) for e in out]
        return ReplayWindow(events=out, earliest_turn=start, total_turns=total)

    def _report_gap(
        self,
        native: tuple[schema.Turn, ...],
        merged: list[schema.Turn | RecordedTurn],
    ) -> None:
        """Log and count a native-history gap the sidecar just healed (#2933).

        Without this the backfill is silent: a reload looks right, and nothing
        says native history is lagging the live stream — the condition behind a
        transcript that "lost" post-restart turns. INFO, not WARNING: the user
        sees every turn, so this is a measurement, not a fault to file.

        Reported once per distinct gap per thread per throttle window. Every
        scrollback page and background sweep re-reads the same gap, and counting
        each read would measure page loads rather than gaps. A gap that changes
        shape (another turn goes missing, or native catches up on one) reports
        again.
        """
        native_ids = {t.id for t in native}
        recorded = [t for t in merged if isinstance(t, RecordedTurn)]
        if not recorded:
            return
        settled = sum(1 for t in recorded if t.turn_id in native_ids)
        missing = len(recorded) - settled
        if not _GAP_LOG.should_log(f"{self._thread_id}:{missing}:{settled}"):
            return
        if missing:
            codex_replay_backfilled_turns_total.labels("missing").inc(missing)
        if settled:
            codex_replay_backfilled_turns_total.labels("settled").inc(settled)
        log_event(log, logging.INFO, "codex.replay.gap_backfilled",
                  session=self._session_id, missing=missing, settled=settled,
                  native_turns=len(native), total_turns=len(merged))

    async def user_prompts(self) -> list[str]:
        """Return the ordered, non-empty human prompts from every turn's
        ``UserMessageItem``\\ s.

        The engine-safe analogue of ``read_user_prompts_from_file`` for Claude
        transcripts (``server/sessions/titles.py``): it goes through the same
        supported ``thread/read`` call ``window()`` uses, rather than parsing
        Codex's private rollout files. Used by the fallback titler
        (``server/codex/titles.py``) since Codex has no on-disk transcript to
        scan for an ``ai-title``-equivalent. Never raises — a read failure
        yields no prompts, the same fail-open contract every other titling
        input honors.
        """
        try:
            window = await self.window(limit=None)
        except Exception:
            return []
        return [e.text.strip() for e in window.events
                if isinstance(e, ev.UserMessage) and e.text.strip()]

    def _session_meta(self, thread: schema.Thread) -> ev.SessionMeta | None:
        model = thread.model or self._model
        if model is None:
            return None
        return ev.SessionMeta(model=model, session_id=thread.id)

    async def events(self, *, max_turns: int | None = 20) -> list:
        window = await self.window(before=None, limit=max_turns)
        return window.events


def _merge_saved_turns(
    native: tuple[schema.Turn, ...], saved: list[RecordedTurn],
) -> list[schema.Turn | RecordedTurn]:
    """Keep native turns, filling holes from ordered live snapshots exactly once.

    A later saved/native match anchors preceding missing turns. For a missing
    tail, UUIDv7 start times place it before any newer native-only turns; opaque
    non-UUID handles retain their existing order. A saved terminal can also
    settle a native turn still reported in progress.
    """
    turns: list[schema.Turn | RecordedTurn] = list(native)

    def insert_missing(turn: RecordedTurn, anchor: int) -> None:
        started = uuid7_ms(turn.turn_id)
        index = next((i for i, candidate in enumerate(turns)
                      if isinstance(candidate, schema.Turn)
                      and started is not None
                      and (when := (uuid7_ms(candidate.id) or _turn_started_ms(candidate))) is not None
                      and when > started), anchor)
        turns.insert(index, turn)

    pending: list[RecordedTurn] = []
    for turn in saved:
        index = next((i for i, candidate in enumerate(turns)
                      if isinstance(candidate, schema.Turn) and candidate.id == turn.turn_id), None)
        if index is None:
            pending.append(turn)
            continue
        for missing in pending:
            insert_missing(missing, index)
            index += 1
        pending = []
        candidate = turns[index]
        if isinstance(candidate, schema.Turn) and candidate.status is schema.TurnStatus.IN_PROGRESS:
            turns[index] = turn
    for turn in pending:
        insert_missing(turn, len(turns))

    return turns


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
