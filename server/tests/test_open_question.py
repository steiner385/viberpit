"""Completion-time open-question detection.

A session that emits the completion marker while its final message still poses a
question to the user must NOT be treated as complete (prod 2026-08-19: two of the
four most recent completion archives ended "Want me to …?" + marker). Detection is
a gated LLM helper (``completion_awaits_user``) that degrades to the existing
prose heuristic (``text_awaits_user``) when the helper is skipped/unavailable —
the LLM verdict, when present, always wins over the heuristic.
"""
from __future__ import annotations

import asyncio
import types

from server.sessions.completion import (
    COMPLETION_MARKER,
    NO_PR_MARKER,
    strip_markers,
)
from server.sessions import open_question as oq


# Verbatim tails of two prod sessions auto-archived on 2026-08-20 despite an open
# question (the bug this module fixes).
PROD_TAIL_PHASES = (
    "Want me to take on Phase 2 (non-rotating chores + every-N-days/biweekly/"
    "quarterly cadences) next, or pause here until these two clear the queue?\n\n"
    + COMPLETION_MARKER
)
PROD_TAIL_FOLLOWUP = (
    "Want me to open a follow-up for either of those?\n\n" + COMPLETION_MARKER
)


def _fake_query(payload: str):
    """query_fn stub yielding one assistant message whose text is *payload*."""
    async def gen(prompt=None, options=None):
        yield types.SimpleNamespace(
            content=[types.SimpleNamespace(text=payload)])
    return gen


def _failing_query(exc: Exception):
    async def gen(prompt=None, options=None):
        raise exc
        yield  # pragma: no cover — makes this an async generator
    return gen


def setup_function(_fn):
    from server.sessions.helper_call import reset_breakers
    reset_breakers()


# ---------------------------------------------------------------- strip_markers

def test_strip_markers_removes_completion_marker():
    assert strip_markers("done!\n" + COMPLETION_MARKER) == "done!"


def test_strip_markers_removes_no_pr_marker():
    assert strip_markers("no PR needed.\n" + NO_PR_MARKER) == "no PR needed."


def test_strip_markers_unmasks_trailing_question():
    # The marker sits AFTER the question, so without stripping, the trailing-"?"
    # heuristic can never fire on a completion turn.
    assert strip_markers(PROD_TAIL_PHASES).endswith("?")


def test_strip_markers_empty_and_none_safe():
    assert strip_markers("") == ""


# ------------------------------------------------------- LLM verdict (primary)

def test_llm_yes_marks_open_question():
    async def run():
        return await oq.completion_awaits_user(
            "All finished. Want me to also do X?",
            query_fn=_fake_query('{"open_question": true}'))
    assert asyncio.run(run()) is True


def test_llm_no_wins_over_broad_heuristic():
    # "let me know" trips the broad heuristic, but the classifier recognises a
    # polite closing — its verdict must win so courteous completions still archive.
    async def run():
        return await oq.completion_awaits_user(
            "Merged and deployed. Let me know if anything looks off.",
            query_fn=_fake_query('{"open_question": false}'))
    assert asyncio.run(run()) is False


def test_llm_json_embedded_in_prose_is_parsed():
    async def run():
        return await oq.completion_awaits_user(
            "Which one do you want?",
            query_fn=_fake_query('Sure: {"open_question": true} there you go'))
    assert asyncio.run(run()) is True


# --------------------------------------------------- degrade to the heuristic

def test_helper_failure_falls_back_to_heuristic_question():
    async def run():
        return await oq.completion_awaits_user(
            strip_markers(PROD_TAIL_FOLLOWUP),
            query_fn=_failing_query(RuntimeError("boom")))
    assert asyncio.run(run()) is True


def test_helper_failure_falls_back_to_heuristic_statement():
    async def run():
        return await oq.completion_awaits_user(
            "PR #42 merged. All tests green.",
            query_fn=_failing_query(RuntimeError("boom")))
    assert asyncio.run(run()) is False


def test_helper_skipped_breaker_open_falls_back(monkeypatch):
    from server.sessions.helper_call import HelperOutcome

    async def _skipped(*a, **k):
        return HelperOutcome(skipped=True)

    monkeypatch.setattr(oq, "run_helper_call", _skipped)
    async def run():
        return await oq.completion_awaits_user("Pick one: A or B?")
    assert asyncio.run(run()) is True


def test_unparseable_llm_output_falls_back():
    async def run():
        return await oq.completion_awaits_user(
            "Should I proceed with the migration?",
            query_fn=_fake_query("I could not decide."))
    assert asyncio.run(run()) is True


def test_empty_text_is_never_a_question():
    async def run():
        return await oq.completion_awaits_user(
            "", query_fn=_fake_query('{"open_question": true}'))
    assert asyncio.run(run()) is False


# ------------------------------------------------- prod regression (heuristic)

def test_prod_tails_detected_by_fallback_heuristic():
    from server.sessions.background_tasks import text_awaits_user
    assert text_awaits_user(strip_markers(PROD_TAIL_PHASES))
    assert text_awaits_user(strip_markers(PROD_TAIL_FOLLOWUP))
