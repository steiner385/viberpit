"""Every tool the assistant's prompt, feature doc and eval cases NAME must exist.

A prompt or doc that tells the model to call a tool the registry no longer has (renamed,
never shipped, typo) fails silently in production: the model "calls" it, gets an
unknown-tool error, and the operator sees a confused answer. This pins the prose to
`TOOL_SPECS`.

What counts as a tool name: a snake_case token whose first segment is a verb some
assistant tool already starts with (``list``, ``get``, ``release``…), derived from the
registry itself so a new verb family is covered the day its first tool lands. Two
families of look-alikes are excluded explicitly rather than by loosening the matcher:
the coding-session tool surface (``set_hold``, ``request_model`` — a different MCP
server, see server/sessions/tools/registry.py) and the few non-tool words below.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from server.assistant import prompt
from server.assistant.tools import TOOL_SPECS
from server.sessions.tools.registry import SESSION_TOOL_SPECS

ROOT = Path(__file__).resolve().parents[2]
FEATURE_DOC = ROOT / "docs/product/features/ai-assistant.md"
EVAL_CASES = ROOT / "docs/superpowers/evals/assistant/cases.jsonl"

# Words that look like `<tool verb>_<noun>` but are not tools: a session status and the
# names of the tests the feature doc cites.
NOT_TOOLS = frozenset({
    "ready_review",
    "test_assistant_prompt", "test_exclusions_match_spec_table",
    "test_feature_doc_tool_counts_match_the_registry",
})

_TOKEN = re.compile(r"(?<![A-Za-z0-9_./-])([a-z]+(?:_[a-z]+)+)(?![A-Za-z0-9_])")


def _prompt_text() -> str:
    return (prompt.ASSISTANT_SYSTEM_PROMPT + prompt.OPERATOR_TOOLS_ENABLED_LINE
            + prompt.OPERATOR_TOOLS_DISABLED_LINE)


def unknown_tool_names(text: str, tools: set[str] | frozenset[str] | None = None) -> list[str]:
    known = set(TOOL_SPECS) if tools is None else set(tools)
    verbs = {name.split("_")[0] for name in known}
    excluded = known | set(SESSION_TOOL_SPECS) | NOT_TOOLS
    return sorted({t for t in _TOKEN.findall(text)
                   if t.split("_")[0] in verbs and t not in excluded})


def test_prompt_names_only_registered_tools():
    assert unknown_tool_names(_prompt_text()) == []


def test_feature_doc_names_only_registered_tools():
    assert unknown_tool_names(FEATURE_DOC.read_text()) == []


def test_eval_cases_name_only_registered_tools():
    missing: set[str] = set()
    for line in EVAL_CASES.read_text().splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        for key in ("expect_tools", "forbid_tools"):
            missing |= {t for t in case.get(key, []) if t not in TOOL_SPECS}
    assert sorted(missing) == []


def test_drift_check_can_fail():
    """The guard must catch a name that is not registered — otherwise the three tests
    above pass vacuously (e.g. if the verb set came out empty)."""
    text = "Call list_sessions, then release_everything_now, then set_hold."
    assert unknown_tool_names(text, {"list_sessions", "release_hold"}) == [
        "release_everything_now"]
    assert unknown_tool_names(_prompt_text() + " get_nonexistent_widget") == [
        "get_nonexistent_widget"]
