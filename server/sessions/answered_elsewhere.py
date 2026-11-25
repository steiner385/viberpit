"""Asks answered outside ViberPit — in the Claude app, over Remote Control.

With Remote Control on, a tool-permission prompt, an ExitPlanMode approval or an
AskUserQuestion can be answered in the Claude web/desktop app instead of in ViberPit.
The CLI then settles the ask itself and sends the host a ``control_cancel_request``;
the SDK cancels our ``can_use_tool`` handler. The host is NOT told the answer: it shows
up later on the stream as the tool_use's tool_result. This module holds the pure half —
the ``via`` marker and reading the outcome out of that tool_result — so the driver and
its tests share one reading of the CLI's wording (claude CLI 2.1.332).
"""
from __future__ import annotations

import re

#: ``PermissionResolved.via`` / ``QuestionResolved.via`` for an ask the CLI settled
#: because someone answered it in the Claude app.
VIA_CLAUDE_APP = "claude_app"

#: The CLI's tool_result openings for a refused tool call. A permission answered
#: "deny" in the Claude app ends with one of these (``is_error`` set); anything else —
#: a successful result or an ordinary tool failure — means the tool ran, i.e. allowed.
_DENIAL_MARKERS = (
    "The user doesn't want to proceed with this tool use.",
    "Permission for this tool use was denied.",
    "User denied permission",
)

# The CLI's AskUserQuestion result lists answers as `"<question>"="<answer>"`, joined
# by ", " and optionally followed per question by ` selected preview:\n…` / ` notes: …`.
# An answer ends at the closing quote that is followed by one of these.
_ANSWER_END = re.compile(r'"(?=, "|\s+selected preview:|\s+notes:|\.(?:\s|$)|$)')
_NO_OPTION = "=(no option selected)"


def result_text(content) -> str:
    """Flatten a tool_result ``content`` (a string or a list of content blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def permission_allowed(ok: bool, content) -> bool:
    """Whether a permission answered in the Claude app was allowed, read from the
    tool_result that followed. Only the CLI's own refusal text counts as a denial: an
    ``is_error`` result without it is a tool that ran and failed."""
    if ok:
        return True
    text = result_text(content).lstrip()
    return not any(text.startswith(m) or m in text[:400] for m in _DENIAL_MARKERS)


def question_answers(content, questions: list[dict]) -> list[list[str] | None]:
    """The labels chosen for each of *questions*, read from the AskUserQuestion
    tool_result, in order. ``None`` for a question with no readable answer.

    A multi-select answer arrives joined with ", "; it is split back into labels only
    when every part is one of that question's options, so a free-text answer that
    happens to contain a comma is kept whole."""
    text = result_text(content)
    out: list[list[str] | None] = []
    for q in questions:
        q_text = q.get("question") or ""
        head = f'"{q_text}"='
        at = text.find(head) if q_text else -1
        if at < 0:
            out.append(None)
            continue
        rest = text[at + len(head):]
        if rest.startswith(_NO_OPTION[1:]) or not rest.startswith('"'):
            out.append(None)
            continue
        end = _ANSWER_END.search(rest, 1)
        answer = rest[1:end.start()] if end else rest[1:]
        if not answer:
            out.append(None)
            continue
        labels = {str(o.get("label")) for o in (q.get("options") or []) if isinstance(o, dict)}
        parts = [p.strip() for p in answer.split(",")]
        if q.get("multiSelect") and len(parts) > 1 and all(p in labels for p in parts):
            out.append(parts)
        else:
            out.append([answer])
    return out
