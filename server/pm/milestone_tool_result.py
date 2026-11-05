"""Session-tool results for the release-milestone ladder: THE plain-words copy for
each refusal code the milestone write routes answer with, and the summary-first result
shape. PM layer, so ``session_triage`` (tick, advance, read) and the note tool share it
without the PM code importing the tools package.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException

from viberpit import brand
from server.mcp_spec import Summarized, ToolArgError
from server.pm.milestone_view import summarize_milestones

#: Plain words for each refusal code the milestone write routes answer with
#: (``{"error": code}``), for a model rather than a person at the page. The web keeps
#: its own copy (``web/src/api/milestones.ts:MILESTONE_ERROR_COPY``); the override
#: codes are absent because no session tool can send an override (R3).
MILESTONE_ERROR_COPY: dict[str, str] = {
    "stale_current": "Someone else advanced this ladder; re-read it with roadmap_milestones.",
    "gate_unmet": (f"The gate is not met yet. Only the owner can advance with an override, "
                   f"from the {brand.NAME} app."),
    "evidence_required": "Add evidence for this tick.",
    "owner_required": "Only the owner can change what someone else recorded.",
    "person_required": "Only a person can make this change, not an automation.",
    "person_or_session_required": "Only a person or a session can write this, not an automation.",
    "ladder_complete": "This ladder is complete. There is nothing left to advance.",
    "no_milestones": "This ladder has no milestones to start.",
    "not_manual": "This condition is measured automatically, so it cannot be ticked.",
    "bad_tick": "Give exactly one of met (true or false) or value (a number).",
    "too_long": "That text is too long. Shorten it and try again.",
    "text_required": "Write the note's text.",
    "bad_kind": "kind must be decision or ask.",
    "bad_status": "That status does not apply to this kind of note.",
    "unknown_milestone": "That milestone is not on the ladder; re-read it with roadmap_milestones.",
    "unknown_condition": "That condition is not in the definition file; re-read the ladder.",
    "unknown_note": "That note no longer exists; re-read the ladder.",
    "nothing_to_update": "There was nothing to change.",
    "no_ladder": "This product has no milestone ladder.",
    "not_found": "No product has that slug.",
    "bad_url": "A link must be a full web address that starts with http:// or https://.",
    "concurrent_update": "Someone else just changed this. Re-read and try again.",
    "automatic_refresh_off": "Automatic refresh is off, so measurements cannot be refreshed now.",
    "refresh_too_soon": "Measurements were refreshed moments ago; try again later.",
}


def milestone_refusal(status: int, code: Any, extra: dict[str, Any] | None = None,
                      fallback: str = "refused") -> ToolArgError:
    """THE refusal text: ``<status> <code>: <plain words>``, plus any extra detail as JSON.
    A code with no copy falls back to *fallback*."""
    words = MILESTONE_ERROR_COPY.get(code) if isinstance(code, str) else None
    text = f"{status} {code}: {words or fallback}"
    return ToolArgError(f"{text} {json.dumps(extra)}" if extra else text)


def milestone_tool_error(exc: HTTPException) -> ToolArgError:
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": exc.detail}
    extra = {k: v for k, v in detail.items() if k not in ("error", "message")}
    return milestone_refusal(exc.status_code, detail.get("error"), extra,
                             str(detail.get("message") or "refused"))


def summarized(view: dict[str, Any], product: str, *, viewer: str | None = None) -> Summarized:
    """A milestones view as the tool result: the text summary first, the view after
    (null for a product with no ladder)."""
    return Summarized(summarize_milestones(view, product=product, viewer=viewer),
                      view if view.get("ladder") else None)
