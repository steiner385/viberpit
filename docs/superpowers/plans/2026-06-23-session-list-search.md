# Session List Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users filter the live sessions list by typing text that matches a session's name or its transcript content.

**Architecture:** A backend endpoint scans each active session's transcript JSONL (resolved via `orchestrator.transcript_path`) plus its name and returns matching session IDs. The frontend debounces the query, fetches matching IDs, and feeds them into the shared `useSessionOrdering` hook which filters *before* grouping/sorting — so both list layouts, grouping, sorting, and keyboard nav stay consistent automatically.

**Tech Stack:** Python FastAPI + pytest (backend) · React + TanStack Query + Vitest + Testing Library (frontend).

## Global Constraints

- Match semantics: **case-insensitive**, query split on whitespace, AND-of-substrings (every term must appear in `name + transcript text`).
- Transcript text = user message text + assistant `text` blocks + `thinking` blocks only. Exclude tool-call/result JSON, base64 images, and `isSidechain` entries.
- No persistence of the search query (unlike group/sort). Box starts empty each visit.
- Search applies to the **active** sessions list only (same population as `GET /api/sessions`). Archived sessions are out of scope.
- Backend route `GET /api/sessions/search` MUST be registered **before** `GET /api/sessions/{session_id}` or FastAPI will match `search` as a `session_id`.
- Auth: every session route uses `_identity: str = Depends(current_identity)`; the new route does too.
- Backend tests run via the prebuilt env, e.g. `<main-checkout>/.venv/bin/python -m pytest server/tests/...`. Web tests run in `web/` with the symlinked `node_modules` (use `./node_modules/.bin/vitest`, not `npx`).

---

### Task 1: Backend transcript-text extraction + match helpers

Pure, dependency-free module so it is trivially unit-testable and reusable.

**Files:**
- Create: `server/sessions/search.py`
- Test: `server/tests/test_session_search.py`

**Interfaces:**
- Consumes: nothing (stdlib `json` only).
- Produces:
  - `transcript_text(jsonl_path: str, *, max_bytes: int = 8 * 1024 * 1024) -> str`
  - `parse_terms(query: str) -> list[str]` — lowercased, whitespace-split, empties dropped.
  - `session_matches(terms: list[str], name: str, text: str) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_session_search.py`:

```python
from pathlib import Path

from server.sessions.search import transcript_text, parse_terms, session_matches


def _write(tmp_path, *lines: str) -> str:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def test_transcript_text_extracts_user_assistant_thinking(tmp_path):
    path = _write(
        tmp_path,
        '{"type":"user","message":{"role":"user","content":"fix the auth bug"}}',
        '{"type":"assistant","message":{"role":"assistant","content":['
        '{"type":"thinking","thinking":"considering tokens"},'
        '{"type":"text","text":"here is the patch"}]}}',
    )
    text = transcript_text(path)
    assert "fix the auth bug" in text
    assert "considering tokens" in text
    assert "here is the patch" in text


def test_transcript_text_excludes_tooljson_images_and_sidechains(tmp_path):
    path = _write(
        tmp_path,
        '{"type":"user","isSidechain":true,"message":{"role":"user","content":"SECRET sidechain"}}',
        '{"type":"assistant","message":{"role":"assistant","content":['
        '{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"NEEDLE_CMD"}}]}}',
        '{"type":"user","message":{"role":"user","content":['
        '{"type":"tool_result","tool_use_id":"t1","content":"NEEDLE_RESULT"},'
        '{"type":"image","source":{"type":"base64","media_type":"image/png","data":"NEEDLE_B64"}}]}}',
    )
    text = transcript_text(path)
    assert "NEEDLE_CMD" not in text
    assert "NEEDLE_RESULT" not in text
    assert "NEEDLE_B64" not in text
    assert "sidechain" not in text


def test_transcript_text_missing_file_returns_empty():
    assert transcript_text("/no/such/file.jsonl") == ""


def test_transcript_text_skips_malformed_lines(tmp_path):
    path = _write(
        tmp_path,
        '{"type":"user","message":{"role":"user","content":"good line"}}',
        "this is not json",
    )
    assert "good line" in transcript_text(path)


def test_parse_terms_lowercases_and_splits():
    assert parse_terms("  Auth   BUG ") == ["auth", "bug"]
    assert parse_terms("") == []


def test_session_matches_requires_all_terms_case_insensitive():
    assert session_matches(["auth", "bug"], "My Session", "fixing the Auth Bug now") is True
    assert session_matches(["auth", "bug"], "Auth work", "no match here") is False  # 'bug' missing
    assert session_matches(["auth"], "AUTH session", "") is True                    # name-only match
    assert session_matches([], "anything", "anything") is True                      # no terms → matches
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest server/tests/test_session_search.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.sessions.search'`.

- [ ] **Step 3: Write the implementation**

Create `server/sessions/search.py`:

```python
"""Pure text-extraction + matching helpers for session list search.

`transcript_text` reads a Claude transcript JSONL and returns ONLY the
human-readable text (user message text, assistant `text` blocks, `thinking`
blocks). Tool-call/result JSON, base64 images, and `isSidechain` entries are
excluded so a substring match means something real. No SQLAlchemy / SDK imports
— this stays cheap and trivially unit-testable.
"""
from __future__ import annotations

import json


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
                for b in _blocks(msg.get("content")):
                    bt = b.get("type")
                    if typ == "user" and bt == "text":
                        parts.append(b.get("text", ""))
                    elif typ == "assistant" and bt == "text":
                        parts.append(b.get("text", ""))
                    elif typ == "assistant" and bt == "thinking":
                        parts.append(b.get("thinking", ""))
    except OSError:
        return ""
    return "\n".join(parts)


def parse_terms(query: str) -> list[str]:
    return [t for t in query.lower().split() if t]


def session_matches(terms: list[str], name: str, text: str) -> bool:
    haystack = f"{name}\n{text}".lower()
    return all(t in haystack for t in terms)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_session_search.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/search.py server/tests/test_session_search.py
git commit -m "feat(search): transcript text extraction + match helpers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Backend `GET /api/sessions/search` endpoint

**Files:**
- Modify: `server/api/sessions.py` (add route immediately after the `/archived` route, before `GET /{session_id}` at ~line 373)
- Test: `server/tests/test_session_search_api.py`

**Interfaces:**
- Consumes: `transcript_text`, `parse_terms`, `session_matches` (Task 1); `orch.list_sessions()` → `list[SessionRecord]`; `await orch.transcript_path(session_id) -> str | None`.
- Produces: `GET /api/sessions/search?q=<text>` → `{"ids": [sessionId, ...]}`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_session_search_api.py`:

```python
"""REST endpoint: GET /api/sessions/search?q=..."""
from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.sessions.types import SessionRecord

H = {"Tailscale-User-Login": "testuser@example.com"}


def _client():
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        app = create_app(orchestrator=orch)
        return TestClient(app, raise_server_exceptions=False), orch


def _rec(sid, name):
    return SessionRecord(id=sid, name=name, account="kindash", claude_session_id=sid,
                         cols=80, rows=24, cwd="/home/u/repo")


def _setup(orch, tmp_path, monkeypatch, records, texts):
    # texts: {session_id: transcript text or None for "no transcript"}
    paths = {}
    for sid, txt in texts.items():
        if txt is None:
            paths[sid] = None
            continue
        p = tmp_path / f"{sid}.jsonl"
        p.write_text('{"type":"user","message":{"role":"user","content":%s}}\n' % _json(txt))
        paths[sid] = str(p)
    monkeypatch.setattr(orch, "list_sessions", lambda: list(records))
    monkeypatch.setattr(orch, "transcript_path", lambda sid: paths.get(sid))


def _json(s):
    import json
    return json.dumps(s)


def test_search_requires_identity():
    client, _ = _client()
    assert client.get("/api/sessions/search?q=hi").status_code == 401


def test_search_matches_name_and_transcript(tmp_path, monkeypatch):
    client, orch = _client()
    records = [_rec("s1", "Auth work"), _rec("s2", "Billing"), _rec("s3", "Docs")]
    _setup(orch, tmp_path, monkeypatch, records, {
        "s1": "nothing relevant here",
        "s2": "we fixed the auth token refresh",
        "s3": None,  # no transcript → name-only
    })
    # 'auth' matches s1 (name) and s2 (transcript), not s3.
    r = client.get("/api/sessions/search?q=auth", headers=H)
    assert r.status_code == 200
    assert sorted(r.json()["ids"]) == ["s1", "s2"]


def test_search_is_and_of_terms(tmp_path, monkeypatch):
    client, orch = _client()
    records = [_rec("s1", "Auth"), _rec("s2", "Auth bug")]
    _setup(orch, tmp_path, monkeypatch, records, {
        "s1": "token refresh",          # has 'auth' (name) but not 'bug'
        "s2": "the bug is fixed",        # has both 'auth' (name) and 'bug'
    })
    r = client.get("/api/sessions/search?q=auth+bug", headers=H)
    assert r.json()["ids"] == ["s2"]


def test_search_blank_query_returns_all_active(tmp_path, monkeypatch):
    client, orch = _client()
    records = [_rec("s1", "A"), _rec("s2", "B")]
    _setup(orch, tmp_path, monkeypatch, records, {"s1": "x", "s2": "y"})
    r = client.get("/api/sessions/search?q=", headers=H)
    assert sorted(r.json()["ids"]) == ["s1", "s2"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_session_search_api.py -q`
Expected: FAIL — 404 (route not found) for the matching tests.

- [ ] **Step 3: Add the route**

In `server/api/sessions.py`, add the import near the top with the other `from server.sessions...` imports:

```python
from server.sessions.search import transcript_text, parse_terms, session_matches
```

Insert this route **immediately after** the `list_archived_sessions` route (the one decorated `@router.get("/archived")`) and **before** `@router.get("/{session_id}")`:

```python
@router.get("/search")
async def search_sessions(
    request: Request,
    q: str = "",
    _identity: str = Depends(current_identity),
) -> dict:
    """Return active session IDs whose name or transcript matches every term in `q`.

    Registered before GET /{session_id} so FastAPI does not match `search` as an id.
    """
    orch = _get_orchestrator(request)
    terms = parse_terms(q)
    records = await orch.list_sessions()

    async def _matches(rec) -> bool:
        text = ""
        if terms:  # blank query short-circuits the file reads
            path = await orch.transcript_path(rec.id)
            if path:
                text = await asyncio.to_thread(transcript_text, path)
        return session_matches(terms, rec.name, text)

    flags = await asyncio.gather(*(_matches(r) for r in records))
    return {"ids": [r.id for r, ok in zip(records, flags) if ok]}
```

(`asyncio`, `Request`, `Depends`, `current_identity`, `_get_orchestrator` are already imported in this file.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest server/tests/test_session_search_api.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Guard against route shadowing — run the existing session API tests**

Run: `.venv/bin/python -m pytest server/tests/test_adopt_api.py server/tests/test_api_rehydrate_list.py -q`
Expected: PASS (no regression in `/{session_id}` routing).

- [ ] **Step 6: Commit**

```bash
git add server/api/sessions.py server/tests/test_session_search_api.py
git commit -m "feat(search): GET /api/sessions/search endpoint

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Frontend debounce + search hook

**Files:**
- Create: `web/src/lib/useDebouncedValue.ts`
- Create: `web/src/session/useSessionSearch.ts`
- Modify: `web/src/api/hooks.ts` (add `QK.sessionSearch`)
- Test: `web/src/session/__tests__/useSessionSearch.test.ts`

**Interfaces:**
- Consumes: `api.get<{ ids: string[] }>` (from `@/api/client`), `QK.sessionSearch`.
- Produces:
  - `useDebouncedValue<T>(value: T, delayMs: number): T`
  - `interface SessionSearch { query: string; setQuery: (q: string) => void; matchedIds: Set<string> | null; isFiltered: boolean }`
  - `useSessionSearch(): SessionSearch`

- [ ] **Step 1: Add the query key**

In `web/src/api/hooks.ts`, add to the `QK` object:

```ts
  sessionSearch: (q: string) => ['session-search', q] as const,
```

- [ ] **Step 2: Write the failing test**

Create `web/src/session/__tests__/useSessionSearch.test.ts`:

```ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { useSessionSearch } from '../useSessionSearch'
import { api } from '@/api/client'

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return createElement(QueryClientProvider, { client: qc }, children)
}

describe('useSessionSearch', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('returns null matchedIds and never fetches for an empty query', () => {
    const spy = vi.spyOn(api, 'get')
    const { result } = renderHook(() => useSessionSearch(), { wrapper })
    expect(result.current.matchedIds).toBeNull()
    expect(result.current.isFiltered).toBe(false)
    expect(spy).not.toHaveBeenCalled()
  })

  it('debounces, fetches, and exposes matched ids as a Set', async () => {
    const spy = vi.spyOn(api, 'get').mockResolvedValue({ ids: ['s1', 's2'] })
    const { result } = renderHook(() => useSessionSearch(), { wrapper })

    act(() => result.current.setQuery('auth'))
    // Before the debounce window elapses, no request.
    expect(spy).not.toHaveBeenCalled()

    await act(async () => { vi.advanceTimersByTime(300) })
    await waitFor(() => expect(result.current.matchedIds).not.toBeNull())

    expect(spy).toHaveBeenCalledWith('/sessions/search?q=auth')
    expect([...(result.current.matchedIds ?? [])].sort()).toEqual(['s1', 's2'])
    expect(result.current.isFiltered).toBe(true)
  })
})
```

- [ ] **Step 3: Run test to verify it fails**

Run (in `web/`): `./node_modules/.bin/vitest run src/session/__tests__/useSessionSearch.test.ts`
Expected: FAIL — cannot resolve `../useSessionSearch`.

- [ ] **Step 4: Write the debounce hook**

Create `web/src/lib/useDebouncedValue.ts`:

```ts
import { useEffect, useState } from 'react'

/** Returns `value` delayed by `delayMs`; resets the timer on each change. */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delayMs)
    return () => clearTimeout(id)
  }, [value, delayMs])
  return debounced
}
```

- [ ] **Step 5: Write the search hook**

Create `web/src/session/useSessionSearch.ts`:

```ts
import { useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import { QK } from '@/api/hooks'
import { useDebouncedValue } from '@/lib/useDebouncedValue'

export interface SessionSearch {
  /** Raw, un-debounced input value (bind to the search box). */
  query: string
  setQuery: (q: string) => void
  /** Matching session ids, or null when no filter is active (empty query / error). */
  matchedIds: Set<string> | null
  /** True when a non-empty query is actively filtering the list. */
  isFiltered: boolean
}

export function useSessionSearch(): SessionSearch {
  const [query, setQuery] = useState('')
  const debounced = useDebouncedValue(query, 250).trim()
  const enabled = debounced !== ''

  const { data, isError } = useQuery({
    queryKey: QK.sessionSearch(debounced),
    queryFn: () => api.get<{ ids: string[] }>(`/sessions/search?q=${encodeURIComponent(debounced)}`),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: 2_000,
  })

  // Null filter when: empty query, first load (no data yet), or request error.
  // A failed/loading search must show the full list, never a misleading "no matches".
  const matchedIds = enabled && data && !isError ? new Set(data.ids) : null
  return { query, setQuery, matchedIds, isFiltered: matchedIds !== null }
}
```

- [ ] **Step 6: Run test to verify it passes**

Run (in `web/`): `./node_modules/.bin/vitest run src/session/__tests__/useSessionSearch.test.ts`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add web/src/lib/useDebouncedValue.ts web/src/session/useSessionSearch.ts web/src/api/hooks.ts web/src/session/__tests__/useSessionSearch.test.ts
git commit -m "feat(search): debounced session-search hook

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Filter inside `useSessionOrdering`

**Files:**
- Modify: `web/src/session/useSessionOrdering.ts`
- Test: `web/src/session/__tests__/useSessionOrdering.test.ts` (add cases)

**Interfaces:**
- Consumes: `Set<string> | null` matched ids (Task 3).
- Produces: `useSessionOrdering(sessions, matchedIds?: Set<string> | null)` — filters before grouping; `groups`/`orderedSessions` reflect the filtered set. Omitting the arg (or `null`) is a no-op.

- [ ] **Step 1: Write the failing tests**

Append to `web/src/session/__tests__/useSessionOrdering.test.ts` (inside the existing `describe`):

```ts
  it('filters sessions to matchedIds before grouping', () => {
    const sessions = [mk('a'), mk('b'), mk('c')]
    const { result } = renderHook(() =>
      useSessionOrdering(sessions, new Set(['a', 'c'])),
    )
    expect(result.current.orderedSessions.map((s) => s.id)).toEqual(['a', 'c'])
    expect(result.current.groups.flatMap((g) => g.sessions).map((s) => s.id)).toEqual(['a', 'c'])
  })

  it('treats null matchedIds as no filter', () => {
    const sessions = [mk('a'), mk('b')]
    const { result } = renderHook(() => useSessionOrdering(sessions, null))
    expect(result.current.orderedSessions.map((s) => s.id)).toEqual(['a', 'b'])
  })

  it('yields empty groups when matchedIds excludes everything', () => {
    const { result } = renderHook(() => useSessionOrdering([mk('a')], new Set<string>()))
    expect(result.current.orderedSessions).toEqual([])
    expect(result.current.groups).toEqual([])
  })
```

- [ ] **Step 2: Run tests to verify they fail**

Run (in `web/`): `./node_modules/.bin/vitest run src/session/__tests__/useSessionOrdering.test.ts`
Expected: FAIL — `useSessionOrdering` takes one argument / filtering not applied.

- [ ] **Step 3: Implement the filter**

In `web/src/session/useSessionOrdering.ts`, change the signature and add a filtering `useMemo`:

```ts
export function useSessionOrdering(
  sessions: readonly Session[],
  matchedIds: Set<string> | null = null,
): SessionOrdering {
  const [groupBy, setGroupBy] = useState<GroupBy>(() => loadSessionOrder().groupBy)
  const [sortBy, setSortBy] = useState<SortBy>(() => loadSessionOrder().sortBy)

  useEffect(() => {
    saveSessionOrder({ groupBy, sortBy })
  }, [groupBy, sortBy])

  const filtered = useMemo(
    () => (matchedIds ? sessions.filter((s) => matchedIds.has(s.id)) : sessions),
    [sessions, matchedIds],
  )

  const groups = useMemo(() => orderSessions(filtered, groupBy, sortBy), [filtered, groupBy, sortBy])
  const orderedSessions = useMemo(() => groups.flatMap((g) => g.sessions), [groups])

  return { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions }
}
```

Required companion change: the default `groupBy` is `'none'`, and `orderSessions([], 'none', …)` currently returns a single empty `'all'` group (`[{ key: 'all', label: null, count: 0, sessions: [] }]`) — so the third test fails without a guard. Add an early return at the top of `orderSessions` in `web/src/session/ordering.ts`:

```ts
export function orderSessions(
  sessions: readonly Session[],
  groupBy: GroupBy,
  sortBy: SortBy,
): SessionGroup[] {
  if (sessions.length === 0) return []
  const sorted = [...sessions].sort(COMPARATORS[sortBy])
  // ...rest unchanged...
```

This is safe: both layouts render `EmptyState`/no-match via `SessionList` *before* consuming `groups` whenever `sessions.length === 0`, so no caller depends on the empty `'all'` group. No existing test asserts that group.

- [ ] **Step 4: Run tests to verify they pass**

Run (in `web/`): `./node_modules/.bin/vitest run src/session/__tests__/useSessionOrdering.test.ts src/lib/__tests__/sessionOrder.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/useSessionOrdering.ts web/src/session/ordering.ts web/src/session/__tests__/useSessionOrdering.test.ts
git commit -m "feat(search): filter sessions by matched ids in useSessionOrdering

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Search input in `SessionListControls`

**Files:**
- Modify: `web/src/components/SessionListControls.tsx`
- Test: `web/src/components/__tests__/SessionListControls.test.tsx` (add cases)

**Interfaces:**
- Consumes: nothing new.
- Produces: `SessionListControls` gains props `searchQuery: string` and `onSearchChange: (q: string) => void`; renders an input `aria-label="Search sessions"` with a clear (✕) button shown only when non-empty.

- [ ] **Step 1: Write the failing test**

Append to `web/src/components/__tests__/SessionListControls.test.tsx`:

```ts
import { useState } from 'react'

function Harness() {
  const [q, setQ] = useState('')
  return (
    <SessionListControls
      groupBy="none" sortBy="status"
      onGroupByChange={() => {}} onSortByChange={() => {}}
      searchQuery={q} onSearchChange={setQ}
    />
  )
}

describe('SessionListControls search', () => {
  it('types into the search box and clears it', async () => {
    render(<Harness />)
    const box = screen.getByLabelText('Search sessions')
    await userEvent.type(box, 'auth')
    expect(box).toHaveValue('auth')
    await userEvent.click(screen.getByLabelText('Clear search'))
    expect(box).toHaveValue('')
  })

  it('hides the clear button when empty', () => {
    render(
      <SessionListControls
        groupBy="none" sortBy="status"
        onGroupByChange={() => {}} onSortByChange={() => {}}
        searchQuery="" onSearchChange={() => {}}
      />,
    )
    expect(screen.queryByLabelText('Clear search')).toBeNull()
  })
})
```

Update the existing first test's `render(<SessionListControls ... />)` to also pass `searchQuery=""` and `onSearchChange={() => {}}` (required props).

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `./node_modules/.bin/vitest run src/components/__tests__/SessionListControls.test.tsx`
Expected: FAIL — search box / clear button not found; type errors on new props.

- [ ] **Step 3: Implement the search input**

Edit `web/src/components/SessionListControls.tsx`:

```tsx
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { GroupBy, SortBy } from '@/session/ordering'

interface SessionListControlsProps {
  groupBy: GroupBy
  sortBy: SortBy
  onGroupByChange: (g: GroupBy) => void
  onSortByChange: (s: SortBy) => void
  searchQuery: string
  onSearchChange: (q: string) => void
}

const selectClass = cn(
  'text-xs rounded-md border border-gray-200 dark:border-gray-700',
  'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300',
  'px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
)

export function SessionListControls({
  groupBy,
  sortBy,
  onGroupByChange,
  onSortByChange,
  searchQuery,
  onSearchChange,
}: SessionListControlsProps) {
  return (
    <div className="flex items-center gap-3 flex-wrap">
      <div className="relative">
        <input
          type="search"
          aria-label="Search sessions"
          placeholder="Search sessions…"
          value={searchQuery}
          onChange={(e) => onSearchChange(e.target.value)}
          className={cn(selectClass, 'pr-7 w-44 [&::-webkit-search-cancel-button]:hidden')}
        />
        {searchQuery !== '' && (
          <button
            type="button"
            aria-label="Clear search"
            onClick={() => onSearchChange('')}
            className="absolute right-1 top-1/2 -translate-y-1/2 p-0.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
          >
            <X size={14} aria-hidden="true" />
          </button>
        )}
      </div>
      <label className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
        <span>Group by</span>
        <select
          aria-label="Group by"
          className={selectClass}
          value={groupBy}
          onChange={(e) => onGroupByChange(e.target.value as GroupBy)}
        >
          <option value="none">None</option>
          <option value="repo">Repo</option>
          <option value="status">Status</option>
        </select>
      </label>
      <label className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
        <span>Sort by</span>
        <select
          aria-label="Sort by"
          className={selectClass}
          value={sortBy}
          onChange={(e) => onSortByChange(e.target.value as SortBy)}
        >
          <option value="status">Status tier</option>
          <option value="lastActive">Last active</option>
          <option value="age">Age</option>
          <option value="name">Name</option>
        </select>
      </label>
    </div>
  )
}
```

(`lucide-react`'s `X` is already used elsewhere in the app — confirm with `grep -rn "from 'lucide-react'" web/src | head`.)

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `./node_modules/.bin/vitest run src/components/__tests__/SessionListControls.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/SessionListControls.tsx web/src/components/__tests__/SessionListControls.test.tsx
git commit -m "feat(search): add search input to SessionListControls

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: "No matches" state in `SessionList`

**Files:**
- Modify: `web/src/components/SessionList.tsx`
- Test: `web/src/components/__tests__/SessionList.test.tsx` (add cases)

**Interfaces:**
- Consumes: `isFiltered` (Task 3's `SessionSearch.isFiltered`), `onClearSearch`.
- Produces: `SessionList` gains props `isFiltered?: boolean` and `onClearSearch?: () => void`. When `sessions.length === 0 && isFiltered` it renders a "No matches" block with a Clear-search button instead of `EmptyState`.

- [ ] **Step 1: Write the failing test**

Append to `web/src/components/__tests__/SessionList.test.tsx` (reuse the file's existing render helpers/imports; this assumes `render` from Testing Library and a `SessionList` import are already present):

```ts
it('shows a no-matches state (not EmptyState) when filtered to zero', async () => {
  const onClearSearch = vi.fn()
  render(
    <SessionList
      sessions={[]}
      accounts={[]}
      isFiltered
      onClearSearch={onClearSearch}
      onNew={() => {}}
      onOpen={() => {}}
      onRename={() => {}}
      onClose={() => {}}
      onDelete={() => {}}
    />,
  )
  expect(screen.getByText(/no sessions match/i)).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: /clear search/i }))
  expect(onClearSearch).toHaveBeenCalled()
})

it('shows EmptyState when there are zero sessions and no active filter', () => {
  render(
    <SessionList
      sessions={[]}
      accounts={[]}
      onNew={() => {}}
      onOpen={() => {}}
      onRename={() => {}}
      onClose={() => {}}
      onDelete={() => {}}
    />,
  )
  expect(screen.queryByText(/no sessions match/i)).toBeNull()
})
```

Ensure the test file imports `vi`, `screen`, and `userEvent` (add to existing imports if missing).

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `./node_modules/.bin/vitest run src/components/__tests__/SessionList.test.tsx`
Expected: FAIL — "no sessions match" text not found.

- [ ] **Step 3: Implement the no-matches branch**

In `web/src/components/SessionList.tsx`, add the two props to `SessionListProps`:

```ts
  isFiltered?: boolean
  onClearSearch?: () => void
```

Add them to the destructured params, then replace the zero-length branch:

```tsx
  if (sessions.length === 0) {
    if (isFiltered) {
      return (
        <div
          className="rounded-lg border border-dashed border-gray-200 dark:border-gray-700 p-6 text-center"
          role="status"
        >
          <p className="text-sm text-gray-500 dark:text-gray-400">No sessions match your search.</p>
          {onClearSearch && (
            <button
              type="button"
              onClick={onClearSearch}
              className="mt-2 text-sm font-medium text-blue-600 dark:text-blue-400 hover:underline"
            >
              Clear search
            </button>
          )}
        </div>
      )
    }
    return <EmptyState onNew={onNew} />
  }
```

- [ ] **Step 4: Run test to verify it passes**

Run (in `web/`): `./node_modules/.bin/vitest run src/components/__tests__/SessionList.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/SessionList.tsx web/src/components/__tests__/SessionList.test.tsx
git commit -m "feat(search): no-matches state in SessionList

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Wire search into both layouts

**Files:**
- Modify: `web/src/components/Dashboard.tsx` (~lines 1037, 1480, 1487)
- Modify: `web/src/components/DesktopShell.tsx` (~lines 103, 372, 379)

**Interfaces:**
- Consumes: `useSessionSearch` (Task 3), `useSessionOrdering(sessions, matchedIds)` (Task 4), the new `SessionListControls` (Task 5) and `SessionList` (Task 6) props.
- Produces: search-wired list in both layouts. No new exports.

- [ ] **Step 1: Wire `Dashboard.tsx`**

After the `useSessions()` line (~1036), add the search hook and pass `matchedIds` into ordering:

```tsx
  const sessionSearch = useSessionSearch()
  const { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions } =
    useSessionOrdering(sessions, sessionSearch.matchedIds)
```

Add the import near the other `@/session` imports:

```tsx
import { useSessionSearch } from '@/session/useSessionSearch'
```

Update the `SessionListControls` usage (~1480):

```tsx
            <SessionListControls
              groupBy={groupBy}
              sortBy={sortBy}
              onGroupByChange={setGroupBy}
              onSortByChange={setSortBy}
              searchQuery={sessionSearch.query}
              onSearchChange={sessionSearch.setQuery}
            />
```

Update the `SessionList` usage (~1487) to add:

```tsx
            isFiltered={sessionSearch.isFiltered}
            onClearSearch={() => sessionSearch.setQuery('')}
```

- [ ] **Step 2: Wire `DesktopShell.tsx`**

Same three edits. After `useSessions()` (~103):

```tsx
  const sessionSearch = useSessionSearch()
  const { groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions } =
    useSessionOrdering(sessions, sessionSearch.matchedIds)
```

Add `import { useSessionSearch } from '@/session/useSessionSearch'` near the other `@/session` imports. Add the two `SessionListControls` props (`searchQuery`, `onSearchChange`) and the two `SessionList` props (`isFiltered`, `onClearSearch`) exactly as in Step 1.

- [ ] **Step 3: Typecheck + full web suite**

Run (in `web/`):
```bash
./node_modules/.bin/tsc --noEmit
./node_modules/.bin/vitest run
./node_modules/.bin/eslint src
```
Expected: tsc clean; all vitest suites PASS; eslint clean.

If any pre-existing Dashboard test breaks because it mocks `@/session/useSessionSearch` partially, add the mock alongside the existing hook mocks in that test's setup (see memory: "Dashboard hooks partial mocks" — adding a hook to Dashboard can break factory-mocked tests that only catch in vitest).

- [ ] **Step 4: Commit**

```bash
git add web/src/components/Dashboard.tsx web/src/components/DesktopShell.tsx
git commit -m "feat(search): wire session search into both list layouts

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Full verification + PR

- [ ] **Step 1: Backend suite**

Run: `.venv/bin/python -m pytest server/tests/ -q`
Expected: PASS (no regressions; new search tests green).

- [ ] **Step 2: Web suite (typecheck, tests, lint)**

Run (in `web/`):
```bash
./node_modules/.bin/tsc --noEmit && ./node_modules/.bin/vitest run && ./node_modules/.bin/eslint src
```
Expected: all green.

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin feat/session-list-search
gh pr create --title "feat(sessions): search/filter the session list by name + transcript" \
  --body "$(cat <<'EOF'
Adds a text search box above the sessions list (both layouts) that filters live
sessions by **name** or **transcript content**.

- Backend `GET /api/sessions/search?q=` scans each active session's transcript
  JSONL (human text only) + name; case-insensitive AND-of-terms.
- Frontend debounces (250ms), feeds matched ids through the shared
  `useSessionOrdering` hook so grouping/sort/keyboard-nav stay consistent.
- Distinct "No matches" state with a Clear-search button; query is not persisted.

Design: docs/superpowers/specs/2026-06-23-session-list-search-design.md
Plan:   docs/superpowers/plans/2026-06-23-session-list-search.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```

Expected: PR opened non-draft, auto-merge armed (per CLAUDE.md completion workflow).

---

## Notes for the implementer

- Run backend pytest against the prebuilt env (`<main-checkout>/.venv/bin/python`), not a bare `python` — a fresh worktree has no built env.
- Web tests need `web/node_modules` symlinked from the deploy worktree; invoke binaries from `./node_modules/.bin`, not `npx`.
- Do not add a search column/index to the DB — transcripts are scanned on demand by design (see spec Non-goals).
- The backend route MUST stay above `GET /{session_id}` in `server/api/sessions.py`.
