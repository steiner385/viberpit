"""Task 9.3: the operator-tier prompt line. `assistant_options()` appends exactly one of
two frozen variants, chosen by its `operator` flag — never a templated sentence, since a
hole would break the prompt's cache-prefix guarantee (server/assistant/prompt.py)."""
from __future__ import annotations

from pathlib import Path

from server.assistant.client import AssistantApi
from server.assistant.options import assistant_options
from server.assistant.prompt import (ASSISTANT_SYSTEM_PROMPT, EXCLUSIONS, EXCLUSIONS_TEXT,
                                     OPERATOR_TOOLS_DISABLED_LINE, OPERATOR_TOOLS_ENABLED_LINE)
from server.assistant.server import build_server

SPEC = Path("docs/superpowers/specs/2026-09-05-ai-assistant-capabilities-v2-design.md")


def _server():
    return build_server(AssistantApi(base_url="http://127.0.0.1:1", token="ccas1:x",
                                     session_id="s1"))


def test_lines_are_frozen_with_no_interpolation_holes():
    assert "{" not in OPERATOR_TOOLS_ENABLED_LINE
    assert "{" not in OPERATOR_TOOLS_DISABLED_LINE


def test_lines_are_distinct_and_nonempty():
    assert OPERATOR_TOOLS_ENABLED_LINE.strip()
    assert OPERATOR_TOOLS_DISABLED_LINE.strip()
    assert OPERATOR_TOOLS_ENABLED_LINE != OPERATOR_TOOLS_DISABLED_LINE


def test_default_overlay_appends_the_disabled_line():
    ov = assistant_options(server=_server(), max_turns=1, model="sonnet")
    assert ov["system_prompt"] == ASSISTANT_SYSTEM_PROMPT + OPERATOR_TOOLS_DISABLED_LINE
    assert "report_gap" in ov["system_prompt"]


def test_operator_overlay_appends_the_enabled_line():
    ov = assistant_options(server=_server(), max_turns=1, model="sonnet", operator=True)
    assert ov["system_prompt"] == ASSISTANT_SYSTEM_PROMPT + OPERATOR_TOOLS_ENABLED_LINE
    assert OPERATOR_TOOLS_DISABLED_LINE not in ov["system_prompt"]


def test_enabled_line_names_every_operator_tool():
    from server.assistant.tools import OPERATOR_TOOLS

    for name in OPERATOR_TOOLS:
        assert name in OPERATOR_TOOLS_ENABLED_LINE, name


def test_exclusions_have_no_interpolation_holes_and_are_in_the_prompt():
    assert "{" not in EXCLUSIONS_TEXT
    assert EXCLUSIONS_TEXT in ASSISTANT_SYSTEM_PROMPT


def test_exclusions_match_spec_table():
    """Task 10.5 (spec v2 §4.11): the exclusion table generates the prompt's redirect
    text, so the two cannot drift. Parses the spec's markdown table and compares its
    Surface/Redirect columns against `EXCLUSIONS` row for row."""
    section = SPEC.read_text().split("### 4.11", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines()
            if line.strip().startswith("|") and "---" not in line]
    data_rows = rows[1:]  # drop the header row ("| Surface | Why excluded | Redirect |")
    parsed = tuple(
        (cells[0], cells[2])
        for cells in (
            [c.strip() for c in row.strip().strip("|").split("|")] for row in data_rows
        )
    )
    assert parsed == EXCLUSIONS
