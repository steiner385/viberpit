"""Task 9.3: the operator-tier prompt line. `assistant_options()` appends exactly one of
two frozen variants, chosen by its `operator` flag — never a templated sentence, since a
hole would break the prompt's cache-prefix guarantee (server/assistant/prompt.py)."""
from __future__ import annotations

from server.assistant.client import AssistantApi
from server.assistant.options import assistant_options
from server.assistant.prompt import (ASSISTANT_SYSTEM_PROMPT, OPERATOR_TOOLS_DISABLED_LINE,
                                     OPERATOR_TOOLS_ENABLED_LINE)
from server.assistant.server import build_server


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
