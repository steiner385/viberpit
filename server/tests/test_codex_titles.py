"""``server.codex.titles.codex_fallback_title`` — a Codex title with no LLM call.

Covers #1279: Codex sessions used to freeze their title on the first prompt's
first 50 characters forever (``CodexAgentDriver.send_prompt``'s ``_titled``
gate) because ``_maybe_apply_auto_name`` bailed out immediately on Codex
sessions — ``transcript_path`` returns ``None`` for any non-Claude engine by
design. This module is the piece that fills that gap without borrowing a
Claude account's credentials (see the module docstring for why that would be
wrong): it picks a title straight out of the session's own prompts.
"""
from __future__ import annotations

from server.codex.titles import codex_fallback_title


def test_picks_the_first_substantive_prompt():
    assert codex_fallback_title(["fix the flaky login test", "also add a test"]) == \
        "fix the flaky login test"


def test_skips_leading_acknowledgement_only_prompts():
    assert codex_fallback_title(["ok", "continue", "refactor the payment retry loop"]) == \
        "refactor the payment retry loop"


def test_all_acknowledgements_falls_back_to_the_first_prompt():
    """Even a conversation that never says anything substantive gets titled,
    from its very first prompt, rather than staying nameless forever."""
    assert codex_fallback_title(["ok", "yes"]) == "ok"


def test_empty_prompts_returns_none():
    assert codex_fallback_title([]) is None


def test_clamps_an_overlong_prompt_on_a_word_boundary():
    long_prompt = "investigate why the nightly deploy pipeline " * 3
    title = codex_fallback_title([long_prompt])
    assert title is not None
    assert len(title) <= 60
    assert not title.endswith(" ")


def test_strips_whitespace_and_quotes():
    assert codex_fallback_title(['  "add dark mode support"  ']) == "add dark mode support"


def test_redacts_a_credential_shaped_secret_out_of_the_title():
    """The title gets persisted, exported, and pushed to every attached client —
    it must go through the same credential-shape redactor as the rest of the
    Codex transcript export, or a prompt containing a live secret becomes a
    session NAME that leaks it everywhere the name is shown."""
    title = codex_fallback_title(["token is sk-livesecrettoken123456", "noted"])
    assert title is not None
    assert "sk-livesecrettoken123456" not in title
    assert "[REDACTED]" in title


def test_only_considers_the_first_thirty_prompts():
    """Mirrors generate_title()'s own _MAX_PROMPTS window: the session's stated
    topic comes from its earliest prompts, not a very late one."""
    prompts = ["ok"] * 30 + ["a much later, unrelated request"]
    assert codex_fallback_title(prompts) == "ok"
