"""A Codex `hand_back` surfaces MID-TURN, as on Claude (#2647 parity, #2713).

The turn-end latch (`TurnEnd.outcome == "hand_back"`) still decides the final
status. These tests pin the early copy: the translator emits `HandBackRequested`
the moment it reads the `dynamicToolCall` item, and the driver parks the reason as
`pending_hand_back_reason` and re-derives status while the turn is still running, so
the orchestrator's `_persist_status` (engine-neutral, it reads the attribute) can
persist and push the ask beside `in_progress`.
"""
from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio

from server.codex import schema
from server.sessions import events as ev
from server.tests.test_codex_driver import THREAD, Harness
from server.tests.test_codex_translator import (_agent_delta, _fresh, _item_completed,
                                                _item_started)


def _hand_back_item(reason: str, *, item_id: str = "hb-1",
                    status: str = "inProgress") -> dict[str, Any]:
    return {"type": "dynamicToolCall", "id": item_id, "tool": "hand_back",
            "status": status, "arguments": {"reason": reason}}


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------


def test_the_translator_emits_the_ask_once_per_call() -> None:
    translator, _ = _fresh()
    started = _item_started(translator, _hand_back_item("pick A or B"))
    assert started == [ev.HandBackRequested(started[0].turn_id, "pick A or B")]  # type: ignore[attr-defined]
    # `item/completed` carries the same call: it must not re-announce the ask.
    assert _item_completed(translator, _hand_back_item("pick A or B",
                                                       status="completed")) == []


def test_a_second_hand_back_with_a_new_reason_replaces_the_first() -> None:
    translator, _ = _fresh()
    _item_started(translator, _hand_back_item("first"))
    out = _item_started(translator, _hand_back_item("second", item_id="hb-2"))
    assert [e.reason for e in out] == ["second"]  # type: ignore[attr-defined]


def test_a_replayed_hand_back_for_another_turn_announces_nothing() -> None:
    translator, _ = _fresh()
    assert _item_completed(translator, _hand_back_item("old", status="completed"),
                           turn_id="tn_old") == []


def test_a_malformed_hand_back_announces_nothing() -> None:
    translator, _ = _fresh()
    assert _item_started(translator, _hand_back_item("   ")) == []


def test_message_prose_is_the_latest_agent_message_only() -> None:
    translator, _ = _fresh()
    _agent_delta(translator, "msg-1", "Looking into it.")
    _agent_delta(translator, "msg-2", "Option A or B? ")
    _agent_delta(translator, "msg-2", "I recommend A.")
    assert not translator.has_signal("hand_back")
    _item_started(translator, _hand_back_item("A or B"))
    assert translator.has_signal("hand_back")
    assert translator.message_prose() == "Option A or B? I recommend A."


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    try:
        yield h
    finally:
        if getattr(h, "driver", None) is not None:
            await h.aclose()


def _notify_hand_back(h: Harness, reason: str, *, turn_id: str = "turn-1") -> None:
    h.server.notify(schema.METHOD_ITEM_STARTED, {
        "threadId": THREAD, "turnId": turn_id, "item": _hand_back_item(reason),
        "startedAtMs": 1})


@pytest.mark.asyncio
async def test_a_hand_back_surfaces_mid_turn_then_latches_at_turn_end(harness) -> None:
    await harness.boot()
    await harness.run_turn()
    statuses_before = len(harness.statuses)

    harness.notify_text("Wire it or remove it?")
    _notify_hand_back(harness, "wire vs remove")
    await harness.settle()

    # Mid-turn: parked, status re-derived, the turn still running.
    assert harness.driver.pending_hand_back_reason == "wire vs remove"
    assert harness.driver.last_hand_back_reason is None
    assert len(harness.statuses) > statuses_before
    assert harness.driver.turn_active
    # Internal: never forwarded to clients, and no card for a turn signal.
    assert harness.of(ev.HandBackRequested) == []
    assert not [c for c in harness.of(ev.ToolCall) if c.name.endswith("hand_back")]

    harness.notify_turn_completed()
    await harness.settle()
    # The latched reason takes over from the mid-turn slot.
    assert harness.driver.last_hand_back_reason == "wire vs remove"
    assert harness.driver.pending_hand_back_reason is None
    assert harness.of(ev.TurnEnd)[-1].outcome == "hand_back"


@pytest.mark.asyncio
async def test_an_interrupt_clears_the_pending_ask(harness) -> None:
    await harness.boot()
    await harness.run_turn()
    _notify_hand_back(harness, "need a decision")
    await harness.settle()
    assert harness.driver.pending_hand_back_reason == "need a decision"

    await harness.driver.interrupt()
    await harness.settle()
    assert harness.driver.pending_hand_back_reason is None


@pytest.mark.asyncio
async def test_the_next_turn_starts_with_no_pending_ask(harness) -> None:
    await harness.boot()
    await harness.run_turn()
    _notify_hand_back(harness, "need a decision")
    await harness.settle()
    await harness.driver.interrupt()
    await harness.settle()
    # A stale value parked directly must still not survive a new turn's start.
    harness.driver._pending_hand_back_reason = "stale"
    await harness.run_turn("next", turn_id="turn-2")
    assert harness.driver.pending_hand_back_reason is None


@pytest.mark.asyncio
async def test_signal_message_prose_reads_the_message_that_carried_the_call(harness) -> None:
    await harness.boot()
    await harness.run_turn()
    # Before the call is read, the answer is "unknown" — never an earlier message.
    assert await harness.driver.signal_message_prose("hand_back", timeout=0.05) is None
    harness.notify_text("Wire it or remove it?")
    _notify_hand_back(harness, "wire vs remove")
    await harness.settle()
    assert await harness.driver.signal_message_prose("hand_back") == "Wire it or remove it?"
