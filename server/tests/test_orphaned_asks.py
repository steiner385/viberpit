"""An interactive request whose waiter died with the daemon is 'orphaned'.

It can never be RESOLVED (the asyncio.Future and the SDK subprocess that awaited
it are both gone) — only RE-ASKED, with the user's choice delivered as the next
turn's prompt. This module is the one definition of that condition.
"""
import pytest

from server.sessions.orphaned_asks import (
    ORPHANABLE_ASK_STATUSES,
    orphaned_ask_kind,
    orphaned_answer_prompt,
)


@pytest.mark.parametrize("status,kind", [
    ("waiting_question", "question"),
    ("waiting_input", "permission"),
    ("ready_review", "plan"),
])
def test_awaiting_human_statuses_map_to_their_ask_kind(status, kind):
    assert orphaned_ask_kind(True, status) == kind


@pytest.mark.parametrize("status", ["in_progress", "condensing", "idle", "done", None])
def test_non_awaiting_statuses_are_not_orphaned_asks(status):
    """Dropped WORK is #938's job (resume_interrupted); only dropped ASKS are ours."""
    assert orphaned_ask_kind(True, status) is None


def test_not_orphaned_unless_the_restart_flag_is_set():
    """A session resting at waiting_question with a LIVE driver is not orphaned —
    interrupted_by_restart is what distinguishes 'the daemon died' from 'still waiting'."""
    assert orphaned_ask_kind(False, "waiting_question") is None


def test_every_orphanable_status_has_a_kind():
    """The set and the mapping must not drift apart."""
    for s in ORPHANABLE_ASK_STATUSES:
        assert orphaned_ask_kind(True, s) is not None


def test_answer_prompt_carries_the_selection_and_corrects_the_false_rejection():
    """The CLI closes out the dangling tool_use with 'The user doesn't want to
    proceed… STOP' (observed in prod, transcript line 128). The assistant reads that
    BEFORE our prompt, so the prompt must explicitly disown it or the assistant stops."""
    text = orphaned_answer_prompt(["Close #15205 as duplicate"])
    assert "Close #15205 as duplicate" in text
    assert "restart" in text.lower()
    assert "not" in text.lower() and "reject" in text.lower()


def test_answer_prompt_renders_multiple_entries_one_per_line():
    """A group of orphaned questions is delivered as ONE prompt carrying
    "<header>: <label>" per question (QuestionGroup.submitAll flattens them,
    because only the first per-question frame can be delivered). A "; " join
    makes that mapping unreadable, so each entry gets its own line."""
    text = orphaned_answer_prompt(["Disposition: Close it", "Owner: Me"])
    assert "- Disposition: Close it" in text
    assert "- Owner: Me" in text
    assert "; " not in text.split("Note:")[0]


def test_answer_prompt_keeps_the_single_entry_form():
    """One answer stays a one-liner — no bullet, no pluralisation."""
    text = orphaned_answer_prompt(["Alpha"])
    assert "My answer: Alpha" in text
    assert "- Alpha" not in text


def test_answer_prompt_handles_an_empty_selection():
    """Never emit a prompt that reads as an answer with nothing in it."""
    text = orphaned_answer_prompt([])
    assert text.strip()
    assert "(no option selected)" in text


def test_answer_prompt_survives_non_string_client_input():
    """``selected`` comes straight off client JSON with no validation
    (server/bridge/ws.py), and the call is not inside a try — a TypeError here
    would kill the WS message loop for the whole socket."""
    text = orphaned_answer_prompt([{"a": 1}, 42])   # type: ignore[list-item]
    assert "42" in text
    assert "a" in text
