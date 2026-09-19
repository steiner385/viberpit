import re

from server.sessions.completion import (
    COMPLETION_MARKER, COMPLETION_INSTRUCTION, commit_trailer,
)
from server.sessions.engine import AgentEngine


def test_marker_is_html_comment():
    # Pinned as a literal, not derived from brand: this test IS the contract, and
    # deriving the expected value from the constant it checks would assert nothing.
    assert COMPLETION_MARKER == "<!-- viberpit:complete -->"


def test_instruction_embeds_the_marker_verbatim():
    # The injected instruction and the detector must reference the same literal.
    assert COMPLETION_MARKER in COMPLETION_INSTRUCTION
    # It should tell the agent to emit it only when genuinely done.
    assert "only" in COMPLETION_INSTRUCTION.lower()


# --- commit attribution -----------------------------------------------------
#
# The Claude trailer used to pin a model version ("Claude Opus 4.8"). Seed
# prompts are built BEFORE the session launches, and ViberPit persists only a
# tier alias ("opus"), so no builder can know the running model's version — it
# is knowable only to the agent, which reports it on every assistant message.
# The trailer therefore delegates: the session fills in its own model.


def test_claude_trailer_pins_no_model_version():
    """The regression this file exists for: a hardcoded version goes stale.

    "Claude Opus 4.8" survived into Opus 5 sessions and misattributed every
    commit an automation seeded. Nothing version-shaped may reappear.
    """
    trailer = commit_trailer(AgentEngine.CLAUDE_CODE)
    assert re.search(r"\d+\.\d+|\bOpus\s*\d|\bSonnet\s*\d|\bHaiku\s*\d", trailer) is None, (
        f"trailer pins a model version and will drift: {trailer!r}"
    )


def test_claude_trailer_delegates_to_the_running_model():
    """It must read as a template the agent substitutes, not a literal to copy."""
    trailer = commit_trailer(AgentEngine.CLAUDE_CODE)
    assert trailer.startswith("Co-Authored-By: Claude ")
    assert trailer.endswith("<noreply@anthropic.com>")
    # A placeholder the agent is meant to replace, in the AGENTS.md spelling.
    assert re.search(r"<[^>@]+>", trailer), f"no fill-in placeholder: {trailer!r}"


def test_codex_trailer_is_unchanged_and_distinct():
    """Engine split predates this change and must survive it: a Codex session is
    never told to co-author as Claude."""
    codex = commit_trailer(AgentEngine.CODEX)
    assert codex == "Co-Authored-By: Codex <noreply@openai.com>"
    assert codex != commit_trailer(AgentEngine.CLAUDE_CODE)


def test_unknown_or_absent_engine_still_gets_the_claude_form():
    """Pre-Phase-7 fallback behavior: anything unrecognized is a Claude session."""
    claude = commit_trailer(AgentEngine.CLAUDE_CODE)
    assert commit_trailer(None) == claude
    assert commit_trailer("nonsense-engine") == claude
    # String values resolve the same as the enum.
    assert commit_trailer("claude_code") == claude
