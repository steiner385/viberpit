"""Codex structured questions (`item/tool/requestUserInput`): Codex's
`AskUserQuestion` equivalent, answered exactly once, through the SAME
``QuestionRegistry``/``ev.Question`` machinery the Claude driver uses.

Every request is answered — never left unanswered like a refused approval —
because the schema has no decline/cancel variant to answer with instead: an
interrupted or stale question is answered with an EMPTY ``answers`` list per
question id, the honest "no answer was given" rather than an invented one.

Driven through the real transport for the same reason `test_codex_approvals.py`
is: the assertions that matter are on the JSON-RPC reply actually written to
the wire, not the driver's internal opinion of it.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_asyncio

from server.codex import schema
from server.sessions import events as ev
from server.tests.test_codex_driver import ACCOUNT, THREAD, Harness

REQUEST_USER_INPUT = schema.METHOD_ITEM_TOOL_REQUEST_USER_INPUT


def _option(label: str, description: str = "") -> dict[str, Any]:
    return {"label": label, "description": description}


def _question(
    question_id: str = "q1",
    *,
    header: str = "Pick a color",
    question: str = "Which color?",
    options: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": question_id,
        "header": header,
        "question": question,
        "options": options,
    }


def _params(
    *,
    thread_id: str = THREAD,
    turn_id: str = "turn-1",
    item_id: str = "item-9",
    is_blocking: bool = True,
    questions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "threadId": thread_id,
        "turnId": turn_id,
        "itemId": item_id,
        "isBlocking": is_blocking,
        "questions": questions
        if questions is not None
        else [
            _question(
                options=[_option("Red", "the red one"), _option("Blue", "the blue one")]
            )
        ],
    }


@pytest_asyncio.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    try:
        yield h
    finally:
        if getattr(h, "driver", None) is not None:
            await h.aclose()


async def _ask(harness: Harness, params: Any = None) -> int:
    """Have the app-server raise one `request_user_input` call and wait for its card."""
    request_id = harness.server.request(
        REQUEST_USER_INPUT, params if params is not None else _params()
    )
    for _ in range(200):
        if harness.driver.pending_questions():
            return request_id
        await asyncio.sleep(0.005)
    raise AssertionError("no question card was raised")


def _qid(harness: Harness) -> str:
    pending = harness.driver.pending_questions()
    assert len(pending) == 1
    return pending[0]["question_id"]


async def _reply(harness: Harness, request_id: int) -> dict[str, Any]:
    return await harness.server.receive_reply(request_id)


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_option_answer_replies_with_the_selected_label(harness):
    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(harness)

    card = harness.of(ev.Question)[-1]
    assert card.header == "Pick a color"
    assert card.question == "Which color?"
    assert card.options == [_option("Red", "the red one"), _option("Blue", "the blue one")]
    assert card.multi_select is False
    assert driver.current_status() == "waiting_question"

    assert driver.answer_question(_qid(harness), ["Blue"], driver.identity, "option")
    reply = await _reply(harness, request_id)

    assert reply["result"] == {"answers": {"q1": {"answers": ["Blue"]}}}
    resolved = harness.of(ev.QuestionResolved)[-1]
    assert resolved.selected == ["Blue"] and resolved.mode == "option"


@pytest.mark.asyncio
async def test_a_freeform_answer_on_an_options_free_question(harness):
    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(
        harness, _params(questions=[_question(header="Name", question="What is your name?")])
    )

    card = harness.of(ev.Question)[-1]
    assert card.options == []

    assert driver.answer_question(_qid(harness), ["Ada"], driver.identity, "freeform")
    reply = await _reply(harness, request_id)

    assert reply["result"] == {"answers": {"q1": {"answers": ["Ada"]}}}


@pytest.mark.asyncio
async def test_a_multi_question_call_is_answered_in_one_batched_reply(harness):
    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(
        harness,
        _params(
            questions=[
                _question("q1", header="A", question="A?"),
                _question("q2", header="B", question="B?"),
            ]
        ),
    )
    for _ in range(200):
        if len(harness.driver.pending_questions()) == 2:
            break
        await asyncio.sleep(0.005)
    else:
        raise AssertionError("both question cards were never raised")

    pending = {p["question"]: p["question_id"] for p in harness.driver.pending_questions()}
    driver.answer_question(pending["A?"], ["yes"], driver.identity, "option")
    driver.answer_question(pending["B?"], ["no"], driver.identity, "option")
    reply = await _reply(harness, request_id)

    assert reply["result"] == {
        "answers": {"q1": {"answers": ["yes"]}, "q2": {"answers": ["no"]}}
    }


@pytest.mark.asyncio
async def test_only_the_owning_identity_can_answer(harness):
    driver = await harness.boot()
    await harness.run_turn("build it")
    await _ask(harness)

    assert driver.answer_question(_qid(harness), ["Blue"], "someone@else", "option") is False
    assert harness.driver.pending_questions()


# ---------------------------------------------------------------------------
# Refusals answer empty, never a guess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_request_for_another_thread_answers_empty_without_a_card(harness):
    """One app-server serves every thread of an account. The manager routes by
    thread id, so this is defence in depth — checked by calling the sink
    directly, because a correct manager can never deliver such a request."""
    driver = await harness.boot()
    response = await driver._on_user_input(
        schema.parse_tool_request_user_input_params(_params(thread_id="other"))
    )

    assert response.answers == {"q1": schema.ToolRequestUserInputAnswer(answers=())}
    assert harness.driver.pending_questions() == []
    assert driver.counters.stale_user_input == 1


@pytest.mark.asyncio
async def test_a_request_from_a_previous_runner_generation_answers_empty(harness):
    driver = await harness.boot()
    harness.manager._runners[ACCOUNT].generation = 42
    request_id = harness.server.request(REQUEST_USER_INPUT, _params())
    await harness.settle()

    assert (await _reply(harness, request_id))["result"] == {
        "answers": {"q1": {"answers": []}}
    }
    assert driver.counters.stale_user_input == 1


@pytest.mark.asyncio
async def test_an_answer_decided_after_the_runner_was_replaced_answers_empty(harness):
    """A human can take minutes. If the app-server was replaced meanwhile, the
    answer describes a request the new process never made."""
    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(harness)

    harness.manager._runners[ACCOUNT].generation = 99
    driver.answer_question(_qid(harness), ["Blue"], driver.identity, "option")
    reply = await _reply(harness, request_id)

    assert reply["result"] == {"answers": {"q1": {"answers": []}}}
    resolved = harness.of(ev.QuestionResolved)[-1]
    assert resolved.selected is None and resolved.mode == "dismissed"
    assert driver.counters.stale_user_input == 1


# ---------------------------------------------------------------------------
# Exit paths — no native request is ever left stranded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_interrupt_answers_every_pending_question_empty(harness):
    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(harness)

    await driver.interrupt()
    reply = await _reply(harness, request_id)

    assert reply["result"] == {"answers": {"q1": {"answers": []}}}
    assert harness.driver.pending_questions() == []
    resolved = harness.of(ev.QuestionResolved)[-1]
    assert resolved.selected is None and resolved.mode == "dismissed"


@pytest.mark.asyncio
async def test_a_crash_notice_answers_every_pending_question_empty(harness):
    from server.codex.manager import RunnerNotice, RunnerNoticeKind

    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(harness)

    driver._on_notice(
        RunnerNotice(kind=RunnerNoticeKind.CRASHED, account=ACCOUNT, generation=1)
    )
    await harness.settle()

    assert (await _reply(harness, request_id))["result"] == {
        "answers": {"q1": {"answers": []}}
    }
    assert harness.driver.pending_questions() == []


@pytest.mark.asyncio
async def test_auth_loss_answers_every_pending_question_empty(harness):
    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(harness)

    harness.server.notify(
        "error",
        {
            "threadId": THREAD,
            "turnId": "turn-1",
            "willRetry": False,
            "error": {"message": "401", "codexErrorInfo": "unauthorized"},
        },
    )
    await harness.settle()

    assert (await _reply(harness, request_id))["result"] == {
        "answers": {"q1": {"answers": []}}
    }
    assert harness.driver.pending_questions() == []


@pytest.mark.asyncio
async def test_closing_the_session_answers_every_pending_question_empty(harness):
    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(harness)

    await driver.aclose()

    assert (await _reply(harness, request_id))["result"] == {
        "answers": {"q1": {"answers": []}}
    }


@pytest.mark.asyncio
async def test_archiving_the_session_answers_every_pending_question_empty(harness):
    driver = await harness.boot()
    await harness.run_turn("build it")
    request_id = await _ask(harness)

    await driver.archive()

    assert (await _reply(harness, request_id))["result"] == {
        "answers": {"q1": {"answers": []}}
    }
