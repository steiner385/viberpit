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
    assert kinds == ["UserMessage", "TurnStart", "TextDelta", "TurnEnd"]
    assert window.events[0].text == "hi"
    assert window.events[2].text == "hello"


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
async def test_an_empty_thread_is_an_empty_window_not_an_error():
    window = await _replay([]).window()
    assert (window.events, window.earliest_turn, window.total_turns) == ([], 0, 0)


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
async def test_an_unknown_item_type_does_not_break_the_replay():
    window = await _replay([
        _turn("t1", extra=[{"type": "somethingNewUpstream", "id": "x1"}])
    ]).window()
    assert [type(e).__name__ for e in window.events] == [
        "UserMessage", "TurnStart", "TextDelta", "TurnEnd"]


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
        "TurnStart", "TextDelta", "TurnEnd"]
