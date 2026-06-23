# Session list search — filter by name & transcript

**Date:** 2026-06-23
**Status:** Draft (design), pending user approval
**Scope:** Backend (`server/`) + frontend (`web/`). Applies to **both** list
layouts — `DesktopShell` (≥1024px) and `Dashboard` (<1024px) — via the shared
`useSessionOrdering` hook and the shared `SessionListControls`.

## Problem

The sessions list shows every active session with no way to narrow it. As the
number of live sessions grows, users want to find a session by typing — not just
by its name, but by **what was said in it** (e.g. recall the session where you
were debugging auth). Searching transcript content is the explicit ask.

## Goals

- Add a text search box above the sessions list (both layouts) that filters the
  visible sessions live as you type.
- A session matches when the query matches its **name** OR its **transcript's
  human-readable text** (user messages, assistant text, thinking).
- **Match semantics:** case-insensitive; the query is split on whitespace and a
  session matches only when **every** term is found somewhere in its name or
  transcript (AND-of-substrings).
- Filtering flows through the shared ordering hook so grouping, sorting, and
  keyboard ↑/↓ navigation all operate over the filtered set automatically.
- Empty query → no filtering (identical to today).
- Non-empty query matching nothing → a distinct **"No matches"** state with a
  clear-search affordance (not the zero-sessions `EmptyState`).

## Non-goals

- Not fuzzy, not regex, not ranked relevance — plain AND-of-substrings.
- No search over **archived** sessions (the "Archived" tab is out of scope; this
  filters the live "Sessions" list only).
- No persistence — the search box starts empty every visit. A stale persisted
  filter silently hiding sessions is a footgun. (Group/sort persist; search does
  not.)
- No highlighting of matched text within transcripts, and no "jump to match"
  inside a session. Search only filters the **list**.
- No new search index / DB column — transcripts are scanned on demand.

## Architecture

### Backend — new search endpoint

`GET /api/sessions/search?q=<text>` → `{ "ids": ["<sessionId>", ...] }`

- Returns the subset of **active** session IDs (same population as
  `GET /api/sessions`) whose name or transcript matches `q`.
- Empty/whitespace-only `q` → `{ "ids": [] }` is never requested by the client
  (the client treats empty query as "no filter"), but the endpoint still handles
  it by returning all active IDs for robustness. (Decision: return all active IDs
  on blank `q` so the endpoint is well-defined; client simply won't call it.)
- Matching is unified on the backend (name **and** transcript) so there is one
  source of truth for match semantics; the frontend just intersects with the
  returned set.

**Transcript text extraction (new helper).**
The existing `server/sessions/replay.py::_parse` already knows the two JSONL
`content` shapes and which blocks are human text, but it (a) builds rich event
objects and (b) trims to the last `max_turns`. Search needs *all* turns and only
the *text*. Add a focused helper rather than overloading `_parse`:

```python
# server/sessions/search.py  (new, pure + unit-tested)
def transcript_text(jsonl_path: str) -> str:
    """Concatenate human-readable text from a Claude transcript JSONL:
    user message text, assistant `text` blocks, and `thinking` blocks.
    Excludes tool-call JSON, tool results, base64 images, UUIDs. Skips
    isSidechain entries. Returns '' on missing/unreadable file."""

def session_matches(query_terms: list[str], name: str, text: str) -> bool:
    """True iff every term (already lowercased) is a substring of
    `(name + '\n' + text).lower()`."""
```

**Endpoint flow** (`server/api/sessions.py`):
1. Parse `q`; lowercase and split on whitespace into `terms`. If no terms,
   return all active IDs.
2. Get active records from the orchestrator (same call `list_sessions` uses).
3. For each record, resolve `orchestrator.transcript_path(session_id)`; a session
   with no resolvable transcript still matches on name alone (text = '').
4. Read + extract via `asyncio.to_thread` (full file). Cap a single transcript
   read at a sane byte ceiling (e.g. 8 MiB) to avoid pathological memory use;
   `errors="ignore"` like `replay`.
5. `session_matches(terms, name, text)` → collect matching IDs.
6. Auth: `Depends(current_identity)`, identical to the other session routes.

Concurrency note: a handful of active sessions × one full-file read each, in a
threadpool, behind a client-side debounce, is the expected load. No caching in
v1; revisit only if profiling shows it matters.

### Frontend — search box + filtered ordering

**`SessionListControls`** gains a controlled search `<input>` (left of the
existing Group by / Sort by selects):
- `searchQuery: string`, `onSearchChange: (q: string) => void` props.
- Clear (✕) button when non-empty; `type="search"`, `aria-label="Search sessions"`.
- Rendered in both layouts exactly where `SessionListControls` already is.

**Debounce + fetch — `useSessionSearch(query)`** (new hook):
- Holds the raw input value; debounces ~250ms to a `debouncedQuery`.
- TanStack Query keyed by `debouncedQuery`, `enabled: debouncedQuery.trim() !== ''`,
  calling `GET /api/sessions/search?q=`. Returns
  `{ matchedIds: Set<string> | null, isSearching: boolean }` where `matchedIds`
  is `null` when the query is empty (→ "no filter").
- `keepPreviousData` so the list doesn't flicker to empty between keystrokes.

**`useSessionOrdering(sessions, matchedIds)`** gains an optional second arg:
- When `matchedIds` is `null` → unchanged behavior.
- When a `Set` → filter `sessions` to those whose `id` is in the set **before**
  `orderSessions(...)`. This keeps `groups`, `orderedSessions`, and the
  `highlightedIndex` keyboard cursor consistent for free.

**Empty-match state.** `SessionList` currently shows `EmptyState` when
`sessions.length === 0`. We must distinguish "no sessions exist" from "search
filtered everything out." Add an `isFiltered?: boolean` prop (true when a
non-empty search is active): when `sessions.length === 0 && isFiltered`, render a
"No sessions match \"<query>\"" message with a **Clear search** button instead of
`EmptyState`. Both `Dashboard` and `DesktopShell` pass `isFiltered` +
`onClearSearch`.

## Data flow

```
input → useSessionSearch (debounce 250ms) → GET /api/sessions/search?q=
      → matchedIds: Set<id> | null
sessions ─┐
          ├→ useSessionOrdering(sessions, matchedIds)
matchedIds ┘     │ filter (if Set) → orderSessions → { groups, orderedSessions }
                 ↓
   SessionList (groups / orderedSessions, isFiltered, onClearSearch)
   + keyboard ↑/↓ over orderedSessions  ← already correct, no extra work
```

## Error handling

- Search request fails (network/500): treat as `matchedIds = null` (show the full
  unfiltered list) rather than an empty list, and surface nothing intrusive — a
  failed search must never look like "no sessions." (Optionally a small inline
  "search unavailable" hint; v1 may simply fall back silently.)
- Unreadable / missing transcript file: that session contributes `text = ''`
  (name-only match). Never 500 the whole search because one file is gone.
- Oversized transcript: capped read; matches found within the cap. (Acceptable;
  documented limitation.)

## Testing

**Backend (`server/tests/`):**
- `transcript_text`: string-form vs list-form `content`; includes user text,
  assistant text, thinking; excludes tool_use/tool_result JSON, base64 image
  data, sidechain entries; missing file → `''`.
- `session_matches`: AND-of-terms, case-insensitivity, name-only match,
  transcript-only match, no match, empty terms.
- Endpoint: auth required (401 without identity), returns matching IDs for a
  fixture set of sessions with known transcripts, blank `q` returns all active.

**Frontend (`web/`):**
- `useSessionOrdering` with `matchedIds`: filters before grouping; group counts
  and `orderedSessions` reflect the filtered set; `null` is a no-op.
- `SessionListControls`: renders search input, fires `onSearchChange`, clear
  button clears.
- `SessionList`: `isFiltered` + zero results → "No matches" + Clear button (not
  `EmptyState`); zero sessions + not filtered → `EmptyState`.
- `useSessionSearch`: debounce gates the fetch; empty query → `matchedIds = null`,
  no request.

## Rollout / sequencing

Backend (pure helper → endpoint → tests) can land first and independently — it
adds a route with no caller. Frontend builds on it. Both ship behind no flag;
the feature is additive and the default (empty query) is a visual no-op.
