"""Pure text-extraction + matching helpers for session list search.

`transcript_text` reads a Claude transcript JSONL and returns ONLY the
human-readable text (user message text, assistant `text` blocks, `thinking`
blocks). Tool-call/result JSON, base64 images, `isSidechain` entries, and the
CLI's own `type:"user"` bookkeeping (harness/meta/compaction records nobody
typed — see `transcript_entries`) are excluded so a substring match means
something real. No SQLAlchemy / SDK imports — this stays cheap and trivially
unit-testable.

`events_text` is its ENGINE-NEUTRAL sibling: the same selection rule applied to
normalized replay events (`server/sessions/events.py` wire dicts) instead of to
Claude's on-disk JSONL. Codex keeps its history inside the app-server and has no
transcript file, so it can only be searched through replay (#1278). The two
functions deliberately agree on what counts as searchable text — one concept,
two input shapes — so a term matches the same content whichever engine ran it.

`search_events` is the in-session search (D-11): the same matcher and skip rules
over one session's full replay, returning positioned hits instead of a yes/no.
"""
from __future__ import annotations

from collections import deque
import json
import re

from .transcript_entries import is_harness_user_entry


def _blocks(content) -> list[dict]:
    # Claude `content` is either a plain string or a list of block dicts.
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def transcript_text(jsonl_path: str, *, max_bytes: int = 8 * 1024 * 1024) -> str:
    parts: list[str] = []
    try:
        with open(jsonl_path, errors="ignore") as f:
            read = 0
            for raw in f:
                read += len(raw)
                if read > max_bytes:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    o = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if o.get("isSidechain"):
                    continue
                typ = o.get("type")
                msg = o.get("message") or {}
                blocks = _blocks(msg.get("content"))
                if typ == "user":
                    # Only index a `type:"user"` entry a human actually typed. The CLI
                    # journals plumbing user entries nobody wrote — the `/model`
                    # `set_model` records (`<command-name>…`, `<local-command-stdout>Set
                    # model to fable…`), a background task's `<task-notification>`,
                    # `isMeta` context, and the auto-compaction summary — which would
                    # otherwise pollute search with noise like "Set model to Fable".
                    # Route through the shared predicate so search agrees with replay and
                    # the titler about what a prompt is.
                    user_text = "\n".join(
                        b.get("text", "") for b in blocks if b.get("type") == "text")
                    if user_text and not is_harness_user_entry(o, user_text):
                        parts.append(user_text)
                elif typ == "assistant":
                    for b in blocks:
                        bt = b.get("type")
                        if bt == "text":
                            parts.append(b.get("text", ""))
                        elif bt == "thinking":
                            parts.append(b.get("thinking", ""))
    except OSError:
        return ""
    return "\n".join(parts)


# Normalized wire-event types that carry text a human wrote or read, mapped to the
# key holding it. Mirrors transcript_text's Claude selection: a `user` entry, an
# assistant `text` block (→ textDelta) and a `thinking` block (→ thinkingDelta).
# toolCall/toolResult stay out for the same reason they do there — matching a term
# inside a tool's JSON argument is not the user finding their own conversation.
_TEXT_EVENTS = {"user": "text", "textDelta": "text", "thinkingDelta": "text"}


def searchable_event(e) -> tuple[str, str] | None:
    """``(type, text)`` of a normalized replay event that carries searchable text, or
    None. The ONE statement of the replay-side skip rules, shared by ``events_text``
    (the list search) and ``search_events`` (in-session search, D-11).

    Skips non-dicts (a caller need not pre-validate a replay it did not build), event
    types with no human-read text, empty text, and sub-agent output
    (``parentToolUseId`` set) — the normalized counterpart of the ``isSidechain``
    entries ``transcript_text`` skips: indexing it would make a session match on text
    its own operator never saw in the main thread."""
    if not isinstance(e, dict):
        return None
    typ = str(e.get("type", ""))
    key = _TEXT_EVENTS.get(typ)
    if key is None or e.get("parentToolUseId"):
        return None
    text = e.get(key)
    if not isinstance(text, str) or not text:
        return None
    return typ, text


def events_text(events) -> str:
    """Extract searchable text from normalized replay events (engine-neutral).

    *events* are wire dicts as produced by ``Event.to_wire()`` — what
    ``ReplayWindow.events`` carries once serialised. Selection is
    ``searchable_event``."""
    parts: list[str] = []
    for e in events or ():
        found = searchable_event(e)
        if found is not None:
            parts.append(found[1])
    return "\n".join(parts)


def parse_terms(query: str) -> list[str]:
    return [t for t in query.lower().split() if t]


def terms_match(terms: list[str], text: str) -> bool:
    """Every term is a case-insensitive substring of *text* — the one matcher both
    searches use."""
    haystack = text.lower()
    return all(t in haystack for t in terms)


def session_matches(terms: list[str], name: str, text: str) -> bool:
    return terms_match(terms, f"{name}\n{text}")


# --- in-session transcript search (D-11) --------------------------------------------

SEARCH_DEFAULT_LIMIT = 200
SEARCH_MAX_LIMIT = 1000
SNIPPET_CHARS = 80
#: The 400 code for a query with no terms.
SEARCH_EMPTY = "SEARCH_EMPTY"

# The in-session search's scope is the list search's minus thinking: a hit must be
# something the transcript shows as the conversation, and the jump lands on it.
_HIT_ROLES = {"user": "user", "textDelta": "assistant"}


def _match_span(text: str, term: str) -> tuple[int, int]:
    """Where *term* (already lowercase) first occurs in *text*, case-insensitively, as
    offsets into *text*. The caller has established it occurs (``terms_match``).

    ``str.lower`` keeps the length for almost all text, so its offsets are *text*'s.
    A character that lowercases to two (``İ``) shifts them, so then each lowered
    character is mapped back to the original it came from."""
    lowered = text.lower()
    pos = lowered.find(term)
    if len(lowered) == len(text):
        return pos, pos + len(term)
    origin = [i for i, c in enumerate(text) for _ in c.lower()]
    return origin[pos], origin[pos + len(term) - 1] + 1


def _snippet(text: str, start: int, end: int) -> tuple[str, int, int]:
    """``(snippet, matchStart, matchEnd)``: up to ``SNIPPET_CHARS`` of *text* around
    ``[start, end)``, trimmed to whole words, on one line (each whitespace character
    becomes one space, so offsets survive). A match longer than ``SNIPPET_CHARS`` is
    the snippet, whole. Offsets are code points here; see ``_utf16``."""
    n = len(text)
    lo, hi = 0, n
    if end - start >= SNIPPET_CHARS:
        lo, hi = start, end
    elif n > SNIPPET_CHARS:
        lo = max(0, start - (SNIPPET_CHARS - (end - start)) // 2)
        hi = min(n, lo + SNIPPET_CHARS)
        lo = max(0, hi - SNIPPET_CHARS)
        if lo > 0 and not text[lo - 1].isspace():
            # Cut mid-word: start after the first space, if one precedes the match.
            m = re.search(r"\s", text[lo:start])
            if m:
                lo += m.end()
        if hi < n and not text[hi].isspace():
            cut = max((i for i in range(end, hi) if text[i].isspace()), default=None)
            if cut is not None:
                hi = cut
    while lo < start and text[lo].isspace():
        lo += 1
    while hi > end and text[hi - 1].isspace():
        hi -= 1
    return re.sub(r"\s", " ", text[lo:hi]), start - lo, end - lo


def _utf16(text: str, index: int) -> int:
    """Code-point *index* into *text* as a UTF-16 code-unit offset: the client slices
    the snippet in JS, where an astral character (an emoji) is two units."""
    return index + sum(1 for c in text[:index] if ord(c) > 0xFFFF)


def search_events(events, terms: list[str], limit: int) -> tuple[list[dict], bool]:
    """In-session search over a FULL normalized replay (D-11): ``(hits, truncated)``.

    An event is a hit when ``searchable_event`` keeps it, it is a user prompt or
    assistant text (never thinking), and ``terms_match`` holds for its text; the
    first term's first occurrence is the hit position. ``_window``'s owner-prefix
    copies are skipped — the prompt at its real position is the hit.

    ``ordinal`` is the paging cursor (``ReplayWindow.earliest_turn`` space) of the
    page holding the hit: an assistant event takes its turn's, a user prompt the NEXT
    TurnStart's (``_window`` slices a prompt into the page of the turn after it; a
    trailing prompt with none after it takes the next ordinal). A TurnStart's stamped
    ``turnIndex`` wins; one without (a live-recorded turn) counts on from the last.
    Sub-agent TurnStarts never move it. A prompt inside an OPEN turn (after its
    TurnStart, before its TurnEnd — a Codex steered prompt) belongs to that turn's
    page, so it takes the current ordinal; a Claude TurnStart/TurnEnd pair always sits
    in one entry, so its prompts are always between turns. Offsets are UTF-16 code
    units. Past *limit* hits the NEWEST *limit* are kept, in transcript order (the
    reader starts at the bottom), and ``truncated`` is True — older matches exist.
    The window is a bounded deque, and snippets are cut only for the kept hits."""
    evs = [e for e in (events or ()) if isinstance(e, dict)]
    # Ordinal + id of each main-thread TurnStart, by position, so a prompt can look
    # ahead to the turn it belongs to.
    starts: list[tuple[int, int, str | None]] = []
    ordinal = -1
    for i, e in enumerate(evs):
        if e.get("type") == "turnStart" and not e.get("parentToolUseId"):
            stamped = e.get("turnIndex")
            ordinal = stamped if isinstance(stamped, int) else ordinal + 1
            starts.append((i, ordinal, e.get("turnId")))
    # (turnId, ordinal, role, text) per match; the snippet is built for survivors only.
    kept: deque[tuple[str | None, int, str, str]] = deque(maxlen=max(limit, 0))
    matched = 0
    nxt = 0                        # index into `starts` of the first start after i
    current: tuple[int, str | None] = (0, None)
    turn_open = False              # between a main-thread TurnStart and its TurnEnd
    for i, e in enumerate(evs):
        while nxt < len(starts) and starts[nxt][0] <= i:
            current = (starts[nxt][1], starts[nxt][2])
            turn_open = True
            nxt += 1
        if e.get("type") == "turnEnd" and not e.get("parentToolUseId"):
            turn_open = False
            continue
        found = searchable_event(e)
        if found is None or e.get("ownerPrefix"):
            continue
        typ, text = found
        role = _HIT_ROLES.get(typ)
        if role is None or not terms_match(terms, text):
            continue
        matched += 1
        if role == "user" and turn_open:
            hit_ordinal = current[0]
            turn_id = e.get("turnId")
        elif role == "user":
            hit_ordinal = starts[nxt][1] if nxt < len(starts) else ordinal + 1
            turn_id = e.get("turnId")
        else:
            hit_ordinal = current[0]
            turn_id = e.get("turnId") or current[1]
        kept.append((turn_id, hit_ordinal, role, text))
    hits: list[dict] = []
    for turn_id, hit_ordinal, role, text in kept:
        snippet, m_start, m_end = _snippet(text, *_match_span(text, terms[0]))
        m_start, m_end = _utf16(snippet, m_start), _utf16(snippet, m_end)
        hits.append({"turnId": turn_id, "ordinal": hit_ordinal, "role": role,
                     "snippet": snippet, "matchStart": m_start, "matchEnd": m_end})
    return hits, matched > len(hits)
