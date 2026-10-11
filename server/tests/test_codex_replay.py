"""`CodexThreadReplay` — history read back over `thread/read`, not off disk.

The contract under test is the one that crosses the browser boundary:
`before`/`limit`/`earliest_turn`/`total_turns` are ordinals in ONE turn space,
shared with the Claude adapter, because the client's scrollback pager is written
against it and knows nothing about engines.
"""
from __future__ import annotations

import pytest

from server.codex import schema
from server.codex.replay import CodexThreadReplay
from server.sessions import events as ev
from server.sessions.replay import ClaudeTranscriptReplay, EngineReplay


class _FakeClient:
    """Answers `thread/read` from a scripted thread payload."""

    def __init__(self, turns: list[dict]) -> None:
        self.turns = turns
        self.calls: list[schema.ThreadReadParams] = []

    async def read_thread(self, params):
        self.calls.append(params)
        return schema.parse_client_response(
            schema.METHOD_THREAD_READ,
            {
                "thread": {
                    "id": "thread-1",
                    "cwd": "/w",
                    "status": {"type": "idle"},
                    "modelProvider": "openai",
                    "createdAt": 0,
                    "updatedAt": 0,
                    "preview": "",
                    "model": "gpt-5-codex",
                    "turns": self.turns,
                }
            },
        )


def _turn(turn_id: str, status: str = "completed", *, text: str = "hi",
          reply: str = "hello", extra: list | None = None) -> dict:
    items: list = [
        {"type": "userMessage", "id": f"{turn_id}-u",
         "content": [{"type": "text", "text": text}]},
        {"type": "agentMessage", "id": f"{turn_id}-a", "text": reply},
    ]
    items.extend(extra or [])
    return {"id": turn_id, "status": status, "items": items}


def _replay(turns: list[dict]) -> CodexThreadReplay:
    return CodexThreadReplay(_FakeClient(turns), "thread-1", model="gpt-5-codex")


def test_it_satisfies_the_engine_replay_protocol():
    assert isinstance(_replay([]), EngineReplay)
    assert isinstance(ClaudeTranscriptReplay("/tmp/x.jsonl"), EngineReplay)


@pytest.mark.asyncio
async def test_a_turn_reads_back_as_prompt_then_turn_then_reply():
    """The prompt must precede the TurnStart. The reducer nests a turn's content
    under the TurnStart that opens it, so a prompt emitted inside the turn would
    render as assistant output."""
    window = await _replay([_turn("t1")]).window()
    kinds = [type(e).__name__ for e in window.events]
    assert kinds == ["SessionMeta", "UserMessage", "TurnStart", "TextDelta", "TurnEnd"]
    assert window.events[1].text == "hi"
    assert window.events[3].text == "hello"


@pytest.mark.asyncio
async def test_thread_read_is_asked_for_turns():
    replay = _replay([_turn("t1")])
    await replay.window()
    assert replay._client.calls[0].include_turns is True


@pytest.mark.asyncio
async def test_turn_start_is_stamped_with_its_ordinal():
    window = await _replay([_turn("t1"), _turn("t2"), _turn("t3")]).window()
    starts = [e for e in window.events if isinstance(e, ev.TurnStart)]
    assert [s.turn_index for s in starts] == [0, 1, 2]
    assert (window.earliest_turn, window.total_turns) == (0, 3)


@pytest.mark.asyncio
async def test_limit_returns_the_last_n_turns_and_reports_the_offset():
    window = await _replay([_turn(f"t{i}") for i in range(5)]).window(limit=2)
    starts = [e for e in window.events if isinstance(e, ev.TurnStart)]
    assert [s.turn_id for s in starts] == ["t3", "t4"]
    assert [s.turn_index for s in starts] == [3, 4]
    assert (window.earliest_turn, window.total_turns) == (3, 5)


@pytest.mark.asyncio
async def test_before_pages_backwards_in_the_same_ordinal_space():
    window = await _replay([_turn(f"t{i}") for i in range(5)]).window(before=3, limit=2)
    starts = [e for e in window.events if isinstance(e, ev.TurnStart)]
    assert [s.turn_index for s in starts] == [1, 2]
    assert window.earliest_turn == 1


@pytest.mark.asyncio
async def test_limit_none_returns_every_turn():
    window = await _replay([_turn(f"t{i}") for i in range(4)]).window(limit=None)
    assert len([e for e in window.events if isinstance(e, ev.TurnStart)]) == 4


@pytest.mark.asyncio
async def test_an_empty_thread_reports_no_turns_but_still_announces_the_model():
    window = await _replay([]).window()
    assert [type(e).__name__ for e in window.events] == ["SessionMeta"]
    assert (window.earliest_turn, window.total_turns) == (0, 0)


@pytest.mark.asyncio
async def test_every_window_leads_with_session_meta():
    """The live SessionMeta is emitted once at thread start and does not survive
    in the driver's turn buffer, so a client attaching to an existing session
    would otherwise never learn which model it is talking to."""
    window = await _replay([_turn(f"t{i}") for i in range(5)]).window(limit=2)
    meta = window.events[0]
    assert isinstance(meta, ev.SessionMeta)
    assert (meta.model, meta.session_id) == ("gpt-5-codex", "thread-1")


@pytest.mark.asyncio
async def test_drop_incomplete_turn_uses_the_authoritative_status():
    """No heuristic: Codex reports TurnStatus, so the in-progress turn the live
    stream owns is dropped exactly, and a completed turn can never be amputated."""
    window = await _replay(
        [_turn("t1"), _turn("t2", status="inProgress")]
    ).window(drop_incomplete_turn=True)
    starts = [e for e in window.events if isinstance(e, ev.TurnStart)]
    assert [s.turn_id for s in starts] == ["t1"]
    assert window.total_turns == 1


@pytest.mark.asyncio
async def test_keeping_an_in_progress_turn_never_closes_it_as_successful():
    window = await _replay([_turn("t1", status="inProgress")]).window()
    end = next(e for e in window.events if isinstance(e, ev.TurnEnd))
    assert end.outcome == "interrupted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,outcome",
    [("completed", "success"), ("failed", "error"), ("interrupted", "interrupted")],
)
async def test_native_status_maps_onto_the_normalized_outcome(status, outcome):
    window = await _replay([_turn("t1", status=status)]).window()
    end = next(e for e in window.events if isinstance(e, ev.TurnEnd))
    assert end.outcome == outcome


@pytest.mark.asyncio
async def test_tool_items_replay_through_the_same_translator_as_the_live_stream():
    """Shared mapping is the point: a reloaded transcript must not differ from the
    one the user watched live."""
    window = await _replay([
        _turn("t1", extra=[{
            "type": "commandExecution", "id": "c1", "command": "ls -la",
            "cwd": "/w", "status": "completed", "exitCode": 0,
            "aggregatedOutput": "total 0",
        }])
    ]).window()
    call = next(e for e in window.events if isinstance(e, ev.ToolCall))
    result = next(e for e in window.events if isinstance(e, ev.ToolResult))
    assert (call.name, call.kind) == ("Bash", ev.ToolKind.BASH)
    assert call.input["command"] == "ls -la"
    assert call.tool_id == result.tool_id == "c1"
    assert result.ok is True


@pytest.mark.asyncio
async def test_replayed_events_carry_the_turns_start_time_not_the_replay_time():
    """`thread/read` items carry no timestamp of their own (verified against
    codex-cli 0.153.2: a commandExecution has `durationMs` but no start), so the
    owning turn's `startedAt` — Unix SECONDS per the app-server schema — is the
    closest real time there is. Before this, every replayed event self-stamped at
    construction (`events.py`'s `_now_ms` default), so a reloaded Codex session
    showed the current time in every gutter."""
    turn = _turn("t1", extra=[{
        "type": "commandExecution", "id": "c1", "command": "ls",
        "cwd": "/w", "status": "completed", "exitCode": 0, "aggregatedOutput": "",
    }])
    turn["startedAt"] = 1_789_404_900
    turn["completedAt"] = 1_789_405_236
    window = await _replay([turn]).window()
    stamped = [e for e in window.events if hasattr(e, "ts")]
    assert {type(e).__name__ for e in stamped} == {
        "UserMessage", "TextDelta", "ToolCall", "ToolResult"}
    assert [e.ts for e in stamped] == [1_789_404_900_000.0] * len(stamped)


@pytest.mark.asyncio
async def test_a_turn_without_a_start_time_replays_unstamped_never_as_now():
    """No `startedAt` means no known time: an empty gutter, never a fabricated one
    (the same contract as the Claude adapter's missing-`timestamp` case)."""
    window = await _replay([_turn("t1")]).window()
    stamped = [e for e in window.events if hasattr(e, "ts")]
    assert stamped
    assert all(e.ts is None for e in stamped)


def _exec(item_id: str) -> dict:
    return {"type": "commandExecution", "id": item_id, "command": "ls", "cwd": "/w",
            "status": "completed", "exitCode": 0, "aggregatedOutput": ""}


def _journal(tmp_path, thread_id: str, *records: dict):
    """Write a sidecar the way the driver's ItemTimesWriter does, then hand back
    the replay bound to it."""
    from server.codex.item_times import ItemTimesWriter, item_times_path
    w = ItemTimesWriter(tmp_path / "codex-item-times")
    for r in records:
        if "s" in r:
            w.record_started(thread_id, r["i"], r["s"])
        if "c" in r:
            w.record_completed(thread_id, r["i"], r["c"])
    return item_times_path(w.root, thread_id)


@pytest.mark.asyncio
async def test_journalled_item_times_beat_the_turns_start(tmp_path):
    """The live stream's `item/started` millisecond is the exact time; the turn's
    `startedAt` is only the fallback for items nothing dated."""
    turn = _turn("t1", extra=[_exec("exec-1"), _exec("exec-2")])
    turn["startedAt"] = 100
    path = _journal(tmp_path, "thread-1",
                    {"i": "exec-1", "s": 100_400, "c": 100_900},
                    {"i": "t1-a", "s": 100_200})
    replay = CodexThreadReplay(_FakeClient([turn]), "thread-1", item_times_path=path)
    window = await replay.window()
    calls = {e.tool_id: e.ts for e in window.events if isinstance(e, ev.ToolCall)}
    results = {e.tool_id: e.ts for e in window.events if isinstance(e, ev.ToolResult)}
    text = next(e for e in window.events if isinstance(e, ev.TextDelta))
    assert calls == {"exec-1": 100_400.0, "exec-2": 100_000.0}
    assert results == {"exec-1": 100_900.0, "exec-2": 100_000.0}
    assert text.ts == 100_200.0


@pytest.mark.asyncio
async def test_a_tool_result_with_no_completed_time_takes_the_items_start(tmp_path):
    turn = _turn("t1", extra=[_exec("exec-1")])
    path = _journal(tmp_path, "thread-1", {"i": "exec-1", "s": 5_000})
    replay = CodexThreadReplay(_FakeClient([turn]), "thread-1", item_times_path=path)
    window = await replay.window()
    result = next(e for e in window.events if isinstance(e, ev.ToolResult))
    assert result.ts == 5_000.0


@pytest.mark.asyncio
async def test_a_uuid7_item_id_dates_itself_when_nothing_journalled_it():
    """`userMessage` ids are UUIDv7: a prompt from before the sidecar existed is
    still exact to the millisecond, and beats the turn's whole-second start."""
    turn = _turn("t1")
    turn["items"][0]["id"] = "01a0a0d7-f021-7343-83aa-383fc474d189"   # 1789404901409 ms
    turn["startedAt"] = 1_789_404_900
    window = await _replay([turn]).window()
    user = next(e for e in window.events if isinstance(e, ev.UserMessage))
    reply = next(e for e in window.events if isinstance(e, ev.TextDelta))
    assert user.ts == 1_789_404_901_409.0
    assert reply.ts == 1_789_404_900_000.0


@pytest.mark.asyncio
async def test_a_turn_with_no_started_at_dates_itself_from_its_uuid7_id():
    turn = _turn("01a0a0d7-edf7-7a91-a852-650c1c5bf4be")   # 1789404900855 ms
    window = await _replay([turn]).window()
    reply = next(e for e in window.events if isinstance(e, ev.TextDelta))
    assert reply.ts == 1_789_404_900_855.0


@pytest.mark.asyncio
async def test_a_missing_sidecar_is_the_same_as_none(tmp_path):
    turn = _turn("t1", extra=[_exec("exec-1")])
    turn["startedAt"] = 7
    replay = CodexThreadReplay(_FakeClient([turn]), "thread-1",
                               item_times_path=tmp_path / "never-written.jsonl")
    window = await replay.window()
    assert all(e.ts == 7_000.0 for e in window.events if hasattr(e, "ts"))


@pytest.mark.asyncio
async def test_each_turn_is_stamped_with_its_own_start_time():
    first, second = _turn("t1"), _turn("t2")
    first["startedAt"], second["startedAt"] = 100, 200
    window = await _replay([first, second]).window()
    by_turn = {e.turn_id: e.ts for e in window.events if isinstance(e, ev.UserMessage)}
    assert by_turn == {"t1-u": 100_000.0, "t2-u": 200_000.0}


@pytest.mark.asyncio
async def test_an_unknown_item_type_does_not_break_the_replay():
    window = await _replay([
        _turn("t1", extra=[{"type": "somethingNewUpstream", "id": "x1"}])
    ]).window()
    assert [type(e).__name__ for e in window.events] == [
        "SessionMeta", "UserMessage", "TurnStart", "TextDelta", "TurnEnd"]


@pytest.mark.asyncio
async def test_events_is_the_windowed_tail():
    events = await _replay([_turn(f"t{i}") for i in range(5)]).events(max_turns=1)
    starts = [e for e in events if isinstance(e, ev.TurnStart)]
    assert [s.turn_id for s in starts] == ["t4"]


@pytest.mark.asyncio
async def test_a_turn_with_no_user_message_still_opens_a_turn():
    """A resumed or automation-started turn can have no prompt item of its own."""
    window = await _replay([
        {"id": "t1", "status": "completed",
         "items": [{"type": "agentMessage", "id": "a1", "text": "solo"}]}
    ]).window()
    assert [type(e).__name__ for e in window.events] == [
        "SessionMeta", "TurnStart", "TextDelta", "TurnEnd"]


@pytest.mark.asyncio
async def test_user_prompts_reads_every_turns_prompt_in_order():
    replay = _replay([
        _turn("t1", text="fix the flaky test"),
        _turn("t2", text="now add a regression test"),
    ])
    assert await replay.user_prompts() == ["fix the flaky test", "now add a regression test"]


@pytest.mark.asyncio
async def test_user_prompts_skips_turns_with_no_user_message():
    replay = _replay([
        {"id": "t1", "status": "completed",
         "items": [{"type": "agentMessage", "id": "a1", "text": "solo"}]},
        _turn("t2", text="the real ask"),
    ])
    assert await replay.user_prompts() == ["the real ask"]


@pytest.mark.asyncio
async def test_user_prompts_empty_thread_returns_empty_list():
    assert await _replay([]).user_prompts() == []


@pytest.mark.asyncio
async def test_user_prompts_never_raises_on_a_read_failure():
    class _BoomClient:
        async def read_thread(self, params):
            raise RuntimeError("app-server is down")

    replay = CodexThreadReplay(_BoomClient(), "thread-1", model="gpt-5-codex")
    assert await replay.user_prompts() == []


@pytest.mark.asyncio
async def test_missing_completed_turns_survive_reload_and_share_cursor_space(tmp_path):
    from server.codex.turn_history import TurnHistoryStore

    root = tmp_path / 'sidecars'
    store = TurnHistoryStore(root)
    for tid in ('t2', 't3'):
        store.record('thread-1', tid, [
            ev.UserMessage(tid, f'prompt {tid}', ts=10), ev.TurnStart(tid),
            ev.TextDelta(tid, f'recovered {tid}', ts=11), ev.TurnEnd(tid),
        ])
    client = _FakeClient([_turn('t1')])
    # A fresh adapter has no live driver buffer, exactly like a navigation reload.
    replay = CodexThreadReplay(client, 'thread-1', item_times_path=root / 'thread-1.jsonl')
    tail = await replay.window(limit=1, drop_incomplete_turn=True)
    assert (tail.earliest_turn, tail.total_turns) == (2, 3)
    assert [(e.turn_id, e.turn_index) for e in tail.events if isinstance(e, ev.TurnStart)] == [('t3', 2)]
    assert any(isinstance(e, ev.TextDelta) and e.text == 'recovered t3' for e in tail.events)
    older = await replay.window(before=2, limit=1)
    assert [(e.turn_id, e.turn_index) for e in older.events if isinstance(e, ev.TurnStart)] == [('t2', 1)]
    assert await replay.user_prompts() == ['hi', 'prompt t2', 'prompt t3']
    # Native history catching up must neither duplicate nor reorder the saved turns.
    client.turns.extend([_turn('t2'), _turn('t3')])
    caught_up = await replay.window(limit=None)
    assert caught_up.total_turns == 3
    assert [e.turn_id for e in caught_up.events if isinstance(e, ev.TurnStart)] == ['t1', 't2', 't3']


@pytest.mark.asyncio
async def test_history_hole_is_inserted_before_next_saved_native_anchor(tmp_path):
    from server.codex.turn_history import TurnHistoryStore

    for tid in ('t2', 't3'):
        TurnHistoryStore(tmp_path).record('thread-1', tid, [
            ev.UserMessage(tid, tid), ev.TurnStart(tid), ev.TextDelta(tid, tid), ev.TurnEnd(tid),
        ])
    replay = CodexThreadReplay(_FakeClient([_turn('t1'), _turn('t3')]), 'thread-1',
                               item_times_path=tmp_path / 'thread-1.jsonl')
    window = await replay.window(limit=None)
    assert [e.turn_id for e in window.events if isinstance(e, ev.TurnStart)] == ['t1', 't2', 't3']


@pytest.mark.asyncio
async def test_partial_saved_history_keeps_native_chronology(tmp_path):
    from server.codex.turn_history import TurnHistoryStore

    ids = [f'01a11264-8aa{i}-7402-b738-2f5b7381c350' for i in range(4)]
    for tid in (ids[1], ids[3]):
        TurnHistoryStore(tmp_path).record('thread-1', tid, [
            ev.UserMessage(tid, tid), ev.TurnStart(tid), ev.TextDelta(tid, tid), ev.TurnEnd(tid),
        ])
    replay = CodexThreadReplay(_FakeClient([_turn(ids[i]) for i in (0, 2, 3)]), 'thread-1',
                               item_times_path=tmp_path / 'thread-1.jsonl')
    window = await replay.window(limit=None)
    assert [e.turn_id for e in window.events if isinstance(e, ev.TurnStart)] == ids


@pytest.mark.asyncio
async def test_saved_tail_precedes_active_native_turn_in_same_second(tmp_path):
    from server.codex.turn_history import TurnHistoryStore
    from server.codex.item_times import uuid7_ms

    earlier = '01a11264-8aa0-7402-b738-2f5b7381c350'
    later = '01a11264-8aa1-7402-b738-2f5b7381c350'
    TurnHistoryStore(tmp_path).record('thread-1', earlier, [
        ev.UserMessage(earlier, 'saved'), ev.TurnStart(earlier), ev.TurnEnd(earlier),
    ])
    active = _turn(later, status='inProgress')
    active['startedAt'] = int(uuid7_ms(later) / 1000)
    replay = CodexThreadReplay(_FakeClient([active]), 'thread-1',
                               item_times_path=tmp_path / 'thread-1.jsonl')
    window = await replay.window(limit=None, drop_incomplete_turn=True)
    assert [e.turn_id for e in window.events if isinstance(e, ev.TurnStart)] == [earlier]
    assert window.total_turns == 1


@pytest.mark.asyncio
async def test_saved_terminal_settles_stale_native_in_progress_turn(tmp_path):
    from server.codex.turn_history import TurnHistoryStore

    TurnHistoryStore(tmp_path).record('thread-1', 't1', [
        ev.UserMessage('temporary-prompt-id', 'saved prompt'), ev.TurnStart('t1'),
        ev.TextDelta('t1', 'finished live'), ev.TurnEnd('t1'),
    ])
    replay = CodexThreadReplay(_FakeClient([_turn('t1', status='inProgress')]), 'thread-1',
                               item_times_path=tmp_path / 'thread-1.jsonl')
    window = await replay.window(drop_incomplete_turn=True)
    assert window.total_turns == 1
    assert any(isinstance(e, ev.TextDelta) and e.text == 'finished live' for e in window.events)
    assert isinstance(window.events[-1], ev.TurnEnd)
    assert window.events[-1].outcome == 'success'


def _gap_records(caplog) -> list[dict]:
    return [r.fields for r in caplog.records
            if getattr(r, "event", None) == "codex.replay.gap_backfilled"]


@pytest.mark.asyncio
async def test_restart_then_n_turns_then_reload_shows_every_turn_and_reports_the_gap(
        tmp_path, caplog):
    """#2933: native history kept only the pre-restart turns while the daemon ran
    six more. A reload must show all of them, and the healed gap must be visible."""
    import logging

    from server.codex import replay as replay_mod
    from server.codex.turn_history import TurnHistoryStore
    from server.obs.metrics import codex_replay_backfilled_turns_total as metric

    replay_mod._GAP_LOG.clear()
    caplog.set_level(logging.INFO, logger="server.codex.replay")
    before = metric.value("missing")
    pre_restart = [_turn(f"pre-{i}") for i in range(3)]
    post_restart = [f"post-{i}" for i in range(6)]
    store = TurnHistoryStore(tmp_path)
    for tid in post_restart:
        store.record("thread-1", tid, [
            ev.UserMessage(tid, f"prompt {tid}"), ev.TurnStart(tid),
            ev.TextDelta(tid, f"work {tid}"), ev.TurnEnd(tid),
        ])
    client = _FakeClient(list(pre_restart))
    replay = CodexThreadReplay(client, "thread-1", session_id="s-1",
                               item_times_path=tmp_path / "thread-1.jsonl")

    window = await replay.window(limit=None, drop_incomplete_turn=True)
    assert window.total_turns == 9
    assert [e.turn_id for e in window.events if isinstance(e, ev.TurnStart)] == [
        *(t["id"] for t in pre_restart), *post_restart]
    assert metric.value("missing") - before == 6
    [record] = _gap_records(caplog)
    assert record == {"session": "s-1", "missing": 6, "settled": 0,
                      "native_turns": 3, "total_turns": 9}

    # Re-reading the same gap (scrollback, a background sweep) is not a new gap.
    await replay.window(before=5, limit=2)
    assert metric.value("missing") - before == 6
    assert len(_gap_records(caplog)) == 1

    # Native catching up leaves nothing to backfill, and nothing to report.
    client.turns.extend(_turn(tid) for tid in post_restart)
    caught_up = await replay.window(limit=None)
    assert caught_up.total_turns == 9
    assert metric.value("missing") - before == 6
    assert len(_gap_records(caplog)) == 1


@pytest.mark.asyncio
async def test_a_gap_that_changes_shape_reports_again_and_settles_count_apart(
        tmp_path, caplog):
    import logging

    from server.codex import replay as replay_mod
    from server.codex.turn_history import TurnHistoryStore
    from server.obs.metrics import codex_replay_backfilled_turns_total as metric

    replay_mod._GAP_LOG.clear()
    caplog.set_level(logging.INFO, logger="server.codex.replay")
    missing0, settled0 = metric.value("missing"), metric.value("settled")
    store = TurnHistoryStore(tmp_path)

    def save(tid: str) -> None:
        store.record("thread-1", tid, [ev.UserMessage(tid, tid), ev.TurnStart(tid),
                                       ev.TurnEnd(tid)])

    save("t1")
    replay = CodexThreadReplay(_FakeClient([_turn("t1", status="inProgress")]), "thread-1",
                               item_times_path=tmp_path / "thread-1.jsonl")
    await replay.window(limit=None)
    assert metric.value("settled") - settled0 == 1
    assert metric.value("missing") - missing0 == 0

    save("t2")
    await replay.window(limit=None)
    assert metric.value("missing") - missing0 == 1
    assert [(r["missing"], r["settled"]) for r in _gap_records(caplog)] == [(0, 1), (1, 1)]


@pytest.mark.asyncio
async def test_a_reload_with_no_gap_reports_nothing(tmp_path, caplog):
    import logging

    from server.codex.turn_history import TurnHistoryStore

    caplog.set_level(logging.INFO, logger="server.codex.replay")
    TurnHistoryStore(tmp_path).record("thread-1", "t1", [
        ev.UserMessage("t1", "t1"), ev.TurnStart("t1"), ev.TurnEnd("t1"),
    ])
    replay = CodexThreadReplay(_FakeClient([_turn("t1")]), "thread-1",
                               item_times_path=tmp_path / "thread-1.jsonl")
    await replay.window(limit=None)
    assert _gap_records(caplog) == []


@pytest.mark.asyncio
async def test_steered_prompts_have_distinct_replay_ids_even_with_identical_text():
    turn = _turn("t1", text="continue")
    turn["items"].extend([
        {"type": "userMessage", "id": "steer-1",
         "content": [{"type": "text", "text": "continue"}]},
        {"type": "agentMessage", "id": "reply-2", "text": "second reply"},
        {"type": "userMessage", "id": "steer-2",
         "content": [{"type": "text", "text": "continue"}]},
        {"type": "agentMessage", "id": "reply-3", "text": "third reply"},
    ])
    window = await _replay([turn]).window()
    users = [e for e in window.events if isinstance(e, ev.UserMessage)]
    assert [e.turn_id for e in users] == ["t1-u", "steer-1", "steer-2"]
    assert [e.text for e in users] == ["continue"] * 3
    assert [e.turn_id for e in window.events if isinstance(e, ev.TextDelta)] == ["t1"] * 3
