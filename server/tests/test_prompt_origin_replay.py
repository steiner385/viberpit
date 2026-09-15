"""Prompt provenance survives the Claude CLI transcript round-trip.

The unit round-trip lives in ``test_prompt_origin.py``. What matters HERE is the seam
that actually broke: replay rebuilds every user turn from the CLI's own JSONL, so an
origin marker that is not split back off at that exact point both leaks into the bubble
as raw text and loses the provenance — the automated prompt reappears as a human message
on the next page load.
"""
from __future__ import annotations

import json

import pytest

from server.sessions import events as ev
from server.sessions.prompt_origin import render_origin_header
from server.sessions.replay import replay_window


def _write(tmp_path, *user_texts: str) -> str:
    """One user+assistant pair per supplied prompt text, as the CLI journals them."""
    p = tmp_path / "t.jsonl"
    lines: list[dict] = []
    for i, text in enumerate(user_texts):
        lines.append({"type": "user", "uuid": f"u{i}", "message": {"content": text}})
        lines.append({"type": "assistant", "uuid": f"a{i},",
                      "message": {"model": "claude-x",
                                  "content": [{"type": "text", "text": f"r{i}"}],
                                  "usage": {"input_tokens": 1}}})
    p.write_text("\n".join(json.dumps(o) for o in lines))
    return str(p)


def _users(window) -> list[ev.UserMessage]:
    return [e for e in window.events if isinstance(e, ev.UserMessage)]


@pytest.mark.asyncio
async def test_journalled_marker_is_split_off_and_recovered(tmp_path):
    decorated = f'{render_origin_header({"kind": "autopilot"})}\nRe-run the failing test.'
    w = await replay_window(_write(tmp_path, decorated), limit=20)
    (user,) = _users(w)
    assert user.text == "Re-run the failing test."          # marker never renders
    assert user.origin == {"kind": "autopilot"}             # provenance survives reload
    assert user.to_wire()["origin"] == {"kind": "autopilot"}


@pytest.mark.asyncio
async def test_a_human_prompt_replays_with_no_origin(tmp_path):
    w = await replay_window(_write(tmp_path, "ship it"), limit=20)
    (user,) = _users(w)
    assert user.text == "ship it"
    # None is what the client keys the right-aligned "you" bubble on — the absence of
    # an origin is load-bearing, so assert it explicitly rather than falsily.
    assert user.origin is None
    assert user.to_wire()["origin"] is None


@pytest.mark.asyncio
async def test_consecutive_identical_nudges_are_both_marked(tmp_path):
    """The context header is deduped against the last one sent; origin is NOT.

    Two autopilot nudges in a row render an identical marker. If the driver deduped it
    the way it dedupes the context header, the second prompt would be journalled bare
    and would rebuild here as a human message — the exact bug, reintroduced for every
    repeat nudge (which is the common case: autopilot nudges a stuck session).
    """
    header = render_origin_header({"kind": "autopilot"})
    w = await replay_window(
        _write(tmp_path, f"{header}\nfirst", f"{header}\nsecond"), limit=20)
    users = _users(w)
    assert [u.text for u in users] == ["first", "second"]
    assert [u.origin for u in users] == [{"kind": "autopilot"}] * 2


@pytest.mark.asyncio
async def test_origin_and_context_headers_both_come_off(tmp_path):
    """An assistant-drawer prompt carries both markers, origin outermost."""
    from server.assistant.context import render_context_header

    decorated = "\n".join([
        render_origin_header({"kind": "assistant"}),
        render_context_header({"route": "/sessions", "sessionId": "abc12345"}, host="dobby"),
        "summarize this",
    ])
    w = await replay_window(_write(tmp_path, decorated), limit=20)
    (user,) = _users(w)
    assert user.text == "summarize this"
    assert user.origin == {"kind": "assistant"}
    assert user.context == {"route": "/sessions", "sessionId": "abc12345"}


@pytest.mark.asyncio
async def test_a_pasted_marker_does_not_forge_provenance(tmp_path):
    """A human quoting a marker mid-prompt keeps their words and stays a human."""
    body = f'why does {render_origin_header({"kind": "autopilot"})} appear?'
    w = await replay_window(_write(tmp_path, body), limit=20)
    (user,) = _users(w)
    assert user.text == body
    assert user.origin is None
