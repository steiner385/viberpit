# Archived List: Grouping, Sorting & Searching — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the archived-sessions list the same class of grouping, sorting, and full-text searching the live list has — by generalizing the live ordering/search machinery into a config-driven engine reused by both lists, and collapsing the duplicated archived list into one shared component.

**Architecture:** Approach C from the spec (`docs/superpowers/specs/2026-06-24-archived-list-grouping-sorting-searching-design.md`). The live `Session` and `ArchivedSession` types differ, so the ordering primitives (`ordering.ts`, `useSessionOrdering`, `SessionListControls`, persistence helpers) are made **generic with a `Session` default** — existing live code keeps the same field names (`group.sessions`, `{kind:'session'}`) and compiles untouched, while archived instantiates the generics with `ArchivedSession`. The backend search endpoint gains a `scope` param and reuses its existing pure helpers.

**Tech Stack:** Python FastAPI + SQLAlchemy backend; React + TypeScript + Vite + Tailwind frontend; pytest (backend), vitest + @testing-library/react (frontend).

## Global Constraints

- **TDD, every task:** write the failing test, watch it fail, implement minimally, watch it pass, commit. One logical change per commit.
- **No parallel implementations (constitution VI):** the generic engine is the single source; live functions become thin wrappers over it. Do not leave a second copy of any ordering/grouping logic.
- **Behaviour-preserving for live:** the existing live ordering/search/controls tests MUST stay green **unchanged** except where a test asserts a literal search URL (Task 7) — that one test is updated deliberately.
- **localStorage keys for the live list are frozen:** namespace `'live'` MUST map to the existing keys `ccswitch.sessionOrder` and `ccswitch.groupCollapse` so users' current prefs survive.
- **Backend test env (worktree):** the worktree has no built venv. Run pytest via the main checkout's prebuilt env with the worktree on `PYTHONPATH`:
  ```bash
  MAIN=/mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher   # prebuilt .venv lives here
  WT=/mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/19bc5911
  cd "$WT" && PYTHONPATH="$WT/src" "$MAIN/.venv/bin/python" -m pytest server/tests/<file> -v
  ```
  If `$MAIN` is wrong, find the env with: `ls /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/*/cc-account-switcher/.venv/bin/python 2>/dev/null`.
- **Web test env (worktree):** `web/node_modules` may be a stale symlink. Before the first web task run: `cd web && rm -f node_modules && npm ci` (registry reachable, ~2s, lockfile committed).
- **Web gates (run all three locally before each web commit):**
  ```bash
  cd web && npx vitest run <file> && npx tsc -b --force && npx eslint <changed files>
  ```
  `tsc --noEmit` checks nothing here — the real type gate is `tsc -b --force`. eslint runs with `--max-warnings 0` in CI.
- **Backend lint gates:** `ruff check server` and `mypy server` (CI runs both before pytest).

---

## File Structure

**Backend**
- Modify: `server/api/sessions.py` — add `scope` param + extract `_search_ids` match-loop helper.
- Modify: `server/tests/test_session_search_api.py` — archived-scope cases.

**Frontend — generic engine (new + generalized)**
- Modify: `web/src/session/ordering.ts` — generic `SessionGroup<T=Session>`, `NavItem<T=Session>`, `OrderingConfig<T>`, `orderItems`, `buildNavItems<T>`; `LIVE_ORDERING` config; `orderSessions` becomes a wrapper.
- Create: `web/src/lib/repoFromCwd.ts` — derive a repo string from a cwd path.
- Create: `web/src/session/archivedOrdering.ts` — `ARCHIVED_ORDERING` config + `ArchGroupBy`/`ArchSortBy` types + option lists + `ARCHIVED_SPEC`.
- Modify: `web/src/lib/sessionOrder.ts` — namespaced `loadOrderPref`/`saveOrderPref`; live wrappers keep old key.
- Modify: `web/src/lib/groupCollapse.ts` — namespaced `loadGroupCollapseNs`/`saveGroupCollapseNs`; live wrappers keep old key.
- Modify: `web/src/session/useSessionOrdering.ts` — generic `useItemOrdering`; `useSessionOrdering` becomes a wrapper; `LIVE_SPEC`.
- Modify: `web/src/session/useSessionSearch.ts` — `scope` param.
- Modify: `web/src/api/hooks.ts` — `QK.sessionSearch(q, scope)`.

**Frontend — shared UI**
- Modify: `web/src/components/SessionListControls.tsx` — generic over group/sort value unions; option lists injectable, default to live.
- Create: `web/src/components/GroupedList.tsx` — generic grouped renderer (headers + collapse), extracted from `SessionList`.
- Modify: `web/src/components/SessionList.tsx` — grouped branch delegates to `GroupedList`.
- Modify: `web/src/components/ArchivedManager.tsx` — controls + ordering + grouped rendering; becomes the single archived list.
- Modify: `web/src/components/Dashboard.tsx` — render `<ArchivedManager/>`; delete the inline archived list/bulk/dialog duplication.

---

## Task 1: Backend — `scope=archived` search

**Files:**
- Modify: `server/api/sessions.py:399-428` (the `search_sessions` endpoint)
- Test: `server/tests/test_session_search_api.py`

**Interfaces:**
- Consumes: `orch.list_sessions()` (live records, `.id`/`.name`), `orch.list_archived()` (list of dicts with `"id"`/`"name"`), `orch.transcript_path(id) -> Awaitable[str|None]`, `orch._resolve_transcript_path(id) -> Awaitable[str|None]`, pure helpers `parse_terms`, `transcript_text`, `session_matches`.
- Produces: `GET /api/sessions/search?q=…&scope=live|archived` → `{"ids": [...]}`. Bad `scope` → 422.

- [ ] **Step 1: Write failing tests** (append to `server/tests/test_session_search_api.py`)

```python
def _setup_archived(orch, tmp_path, monkeypatch, rows, texts):
    # rows: list of {"id","name"} dicts; texts: {id: transcript text or None}
    paths = {}
    for sid, txt in texts.items():
        if txt is None:
            paths[sid] = None
            continue
        p = tmp_path / f"arch-{sid}.jsonl"
        p.write_text('{"type":"user","message":{"role":"user","content":%s}}\n' % _json(txt))
        paths[sid] = str(p)

    async def _list_archived():
        return list(rows)

    async def _resolve(sid):
        return paths.get(sid)

    monkeypatch.setattr(orch, "list_archived", _list_archived)
    monkeypatch.setattr(orch, "_resolve_transcript_path", _resolve)


def test_search_archived_matches_name_and_transcript(tmp_path, monkeypatch):
    client, orch = _client()
    # Live population must NOT leak into archived results.
    monkeypatch.setattr(orch, "list_sessions", lambda: [_rec("live1", "auth live")])
    monkeypatch.setattr(orch, "transcript_path", lambda sid: None)
    rows = [{"id": "a1", "name": "Auth cleanup"}, {"id": "a2", "name": "Billing"}, {"id": "a3", "name": "Docs"}]
    _setup_archived(orch, tmp_path, monkeypatch, rows, {
        "a1": "nothing here",
        "a2": "we fixed the auth token refresh",
        "a3": None,
    })
    r = client.get("/api/sessions/search?q=auth&scope=archived", headers=H)
    assert r.status_code == 200
    assert sorted(r.json()["ids"]) == ["a1", "a2"]  # live1 excluded


def test_search_archived_blank_returns_all_archived(tmp_path, monkeypatch):
    client, orch = _client()
    monkeypatch.setattr(orch, "list_sessions", lambda: [_rec("live1", "x")])
    monkeypatch.setattr(orch, "transcript_path", lambda sid: None)
    rows = [{"id": "a1", "name": "A"}, {"id": "a2", "name": "B"}]
    _setup_archived(orch, tmp_path, monkeypatch, rows, {"a1": "x", "a2": "y"})
    r = client.get("/api/sessions/search?q=&scope=archived", headers=H)
    assert sorted(r.json()["ids"]) == ["a1", "a2"]


def test_search_live_scope_excludes_archived(tmp_path, monkeypatch):
    client, orch = _client()
    records = [_rec("s1", "auth")]
    _setup(orch, tmp_path, monkeypatch, records, {"s1": "x"})
    # archived rows present but must not appear under default/live scope
    async def _list_archived():
        return [{"id": "a1", "name": "auth archived"}]
    monkeypatch.setattr(orch, "list_archived", _list_archived)
    r = client.get("/api/sessions/search?q=auth", headers=H)  # default scope=live
    assert r.json()["ids"] == ["s1"]


def test_search_bad_scope_is_422():
    client, _ = _client()
    assert client.get("/api/sessions/search?q=hi&scope=nope", headers=H).status_code == 422
```

- [ ] **Step 2: Run, verify they fail**

```bash
cd "$WT" && PYTHONPATH="$WT/src" "$MAIN/.venv/bin/python" -m pytest server/tests/test_session_search_api.py -v
```
Expected: the 4 new tests FAIL (422 not raised; archived branch missing → `scope` ignored, archived ids empty/wrong).

- [ ] **Step 3: Implement** — replace the body of `search_sessions` in `server/api/sessions.py:399-428`. Add `from typing import Literal` at the top of the file if absent.

```python
async def _search_ids(items, resolve, terms) -> list[str]:
    """items: list[(id, name)]; resolve: async (id)->path|None. Returns matching ids.

    The transcript file is read here in the edge process (see note below) for each
    candidate; blank `terms` skips the reads entirely.
    """
    async def _matches(sid: str, name: str) -> bool:
        text = ""
        if terms:
            path = await resolve(sid)
            if path:
                # Read happens in the edge process (not the daemon). Safe because the
                # daemon shares the host filesystem over a local Unix socket. If the
                # daemon is ever split to another host, move this behind a daemon RPC.
                text = await asyncio.to_thread(transcript_text, path)
        return session_matches(terms, name, text)

    flags = await asyncio.gather(*(_matches(i, n) for i, n in items))
    return [i for (i, _), ok in zip(items, flags) if ok]


@router.get("/search")
async def search_sessions(
    request: Request,
    q: str = "",
    scope: Literal["live", "archived"] = "live",
    _identity: str = Depends(current_identity),
) -> dict:
    """Return session IDs whose name or transcript matches every term in `q`.

    `scope=live` (default) searches active sessions; `scope=archived` searches
    archived rows. Registered before GET /{session_id} so FastAPI does not match
    `search` as an id.
    """
    orch = _get_orchestrator(request)
    terms = parse_terms(q)
    if scope == "archived":
        rows = await orch.list_archived()
        items = [(r["id"], r["name"]) for r in rows]
        ids = await _search_ids(items, orch._resolve_transcript_path, terms)
    else:
        records = await orch.list_sessions()
        items = [(r.id, r.name) for r in records]
        ids = await _search_ids(items, orch.transcript_path, terms)
    return {"ids": ids}
```

- [ ] **Step 4: Run all search tests, verify pass**

```bash
cd "$WT" && PYTHONPATH="$WT/src" "$MAIN/.venv/bin/python" -m pytest server/tests/test_session_search_api.py server/tests/test_session_search.py -v
```
Expected: all PASS (old live cases + 4 new archived/scope cases).

- [ ] **Step 5: Lint + commit**

```bash
cd "$WT" && PYTHONPATH="$WT/src" "$MAIN/.venv/bin/python" -m ruff check server/api/sessions.py && PYTHONPATH="$WT/src" "$MAIN/.venv/bin/python" -m mypy server/api/sessions.py
git add server/api/sessions.py server/tests/test_session_search_api.py
git commit -m "feat(search): scope=archived on /api/sessions/search

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Frontend — generic ordering engine (live behaviour-preserving)

**Files:**
- Modify: `web/src/session/ordering.ts`
- Test: `web/src/session/__tests__/ordering.generic.test.ts` (new)

**Interfaces:**
- Produces:
  - `interface SessionGroup<T = Session> { key: string; label: string | null; count: number; sessions: T[] }`
  - `type NavItem<T = Session> = { kind: 'header'; key; label; count; collapsed } | { kind: 'session'; session: T }`
  - `interface GroupDef<T> { bucketOf: (item: T) => { key: string; label: string; order?: number } }`
  - `interface OrderingConfig<T> { idOf: (item: T) => string; groupings: Record<string, GroupDef<T>>; sorts: Record<string, (a: T, b: T) => number> }`
  - `function orderItems<T>(items: readonly T[], groupBy: string, sortBy: string, config: OrderingConfig<T>): SessionGroup<T>[]`
  - `function buildNavItems<T>(groups: readonly SessionGroup<T>[], collapsedKeys: ReadonlySet<string>): NavItem<T>[]`
  - `const LIVE_ORDERING: OrderingConfig<Session>`
  - unchanged signatures: `orderSessions(sessions, groupBy: GroupBy, sortBy: SortBy): SessionGroup[]`, `GroupBy`, `SortBy`.
- Consumes: `formatRepoLabel` (`@/lib/repoLabel`), `triageTier`/`triageRank` (`./triageTier`), `TIER_RANK`/`TIER_LABEL` (`./statusTier`), `COMPARATORS` (kept internal).

- [ ] **Step 1: Write failing test** (`web/src/session/__tests__/ordering.generic.test.ts`)

```ts
import { describe, it, expect } from 'vitest'
import { orderItems, type OrderingConfig } from '../ordering'

interface Row { id: string; acct: string; t: number }
const CONFIG: OrderingConfig<Row> = {
  idOf: (r) => r.id,
  groupings: {
    acct: { bucketOf: (r) => ({ key: r.acct, label: r.acct }) },
  },
  sorts: {
    time: (a, b) => a.t - b.t,
    id: (a, b) => a.id.localeCompare(b.id),
  },
}

describe('orderItems (generic)', () => {
  const rows: Row[] = [
    { id: 'b', acct: 'y', t: 3 },
    { id: 'a', acct: 'x', t: 1 },
    { id: 'c', acct: 'x', t: 2 },
  ]

  it('groupBy none → single null-label group, sorted', () => {
    const g = orderItems(rows, 'none', 'time', CONFIG)
    expect(g).toHaveLength(1)
    expect(g[0].label).toBeNull()
    expect(g[0].sessions.map((r) => r.id)).toEqual(['a', 'c', 'b'])
  })

  it('groups by config bucket, buckets ordered by label, items keep sort order', () => {
    const g = orderItems(rows, 'acct', 'time', CONFIG)
    expect(g.map((x) => x.key)).toEqual(['x', 'y'])
    expect(g[0].sessions.map((r) => r.id)).toEqual(['a', 'c'])
    expect(g[0].count).toBe(2)
  })

  it('empty input → empty array', () => {
    expect(orderItems([], 'acct', 'time', CONFIG)).toEqual([])
  })
})
```

- [ ] **Step 2: Run, verify fail**

```bash
cd web && npx vitest run src/session/__tests__/ordering.generic.test.ts
```
Expected: FAIL — `orderItems` not exported.

- [ ] **Step 3: Implement** — rewrite `web/src/session/ordering.ts`. Keep `COMPARATORS`, `byId`, `lastActiveMs` as-is; generalize the rest:

```ts
import type { Session } from '@/api/types'
import { TIER_RANK, TIER_LABEL } from './statusTier'
import { triageTier, triageRank } from './triageTier'
import { formatRepoLabel } from '@/lib/repoLabel'
import { isValidTimestamp } from '@/lib/relativeTime'

export type GroupBy = 'none' | 'repo' | 'status'
export type SortBy = 'status' | 'lastActive' | 'age' | 'name'

export interface SessionGroup<T = Session> {
  key: string
  label: string | null
  count: number
  sessions: T[]
}

export interface GroupDef<T> {
  /** Map an item to its bucket. `order` sorts buckets (lower first); ties break on label. */
  bucketOf: (item: T) => { key: string; label: string; order?: number }
}

export interface OrderingConfig<T> {
  idOf: (item: T) => string
  /** Keyed by GroupBy value. 'none' is implicit (single null-label group). */
  groupings: Record<string, GroupDef<T>>
  /** Keyed by SortBy value. */
  sorts: Record<string, (a: T, b: T) => number>
}

const byId = (a: Session, b: Session): number => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)
const lastActiveMs = (iso: string | null | undefined): number | null =>
  isValidTimestamp(iso) ? new Date(iso).getTime() : null

const COMPARATORS: Record<SortBy, (a: Session, b: Session) => number> = {
  status: (a, b) => triageRank(a) - triageRank(b),
  lastActive: (a, b) => {
    const ta = lastActiveMs(a.updatedAt)
    const tb = lastActiveMs(b.updatedAt)
    if (ta === null && tb === null) return byId(a, b)
    if (ta === null) return 1
    if (tb === null) return -1
    return tb - ta || byId(a, b)
  },
  age: (a, b) => new Date(a.createdAt).getTime() - new Date(b.createdAt).getTime() || byId(a, b),
  name: (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }) || byId(a, b),
}

/** Generic group+sort engine. Buckets via config.groupings[groupBy]; 'none' = flat. */
export function orderItems<T>(
  items: readonly T[],
  groupBy: string,
  sortBy: string,
  config: OrderingConfig<T>,
): SessionGroup<T>[] {
  if (items.length === 0) return []
  const sorted = [...items].sort(config.sorts[sortBy])
  const def = config.groupings[groupBy]
  if (groupBy === 'none' || !def) {
    return [{ key: 'all', label: null, count: sorted.length, sessions: sorted }]
  }
  const buckets = new Map<string, { label: string; order: number; sessions: T[] }>()
  for (const it of sorted) {
    const b = def.bucketOf(it)
    const cur = buckets.get(b.key) ?? { label: b.label, order: b.order ?? 0, sessions: [] }
    cur.sessions.push(it)
    buckets.set(b.key, cur)
  }
  return [...buckets.entries()]
    .sort(
      ([, a], [, b]) =>
        a.order - b.order || a.label.localeCompare(b.label, undefined, { sensitivity: 'base' }),
    )
    .map(([key, b]) => ({ key, label: b.label, count: b.sessions.length, sessions: b.sessions }))
}

/** Live-session ordering: repo + status grouping, status/lastActive/age/name sorts. */
export const LIVE_ORDERING: OrderingConfig<Session> = {
  idOf: (s) => s.id,
  groupings: {
    repo: {
      bucketOf: (s) => {
        const rl = formatRepoLabel(s.repo)
        return { key: rl.text, label: rl.text, order: rl.isScratch ? 1 : 0 }
      },
    },
    status: {
      bucketOf: (s) => {
        const tier = triageTier(s)
        return { key: tier, label: TIER_LABEL[tier], order: TIER_RANK[tier] }
      },
    },
  },
  sorts: COMPARATORS,
}

/** Live wrapper — unchanged signature; existing callers/tests keep working. */
export function orderSessions(
  sessions: readonly Session[],
  groupBy: GroupBy,
  sortBy: SortBy,
): SessionGroup[] {
  return orderItems(sessions, groupBy, sortBy, LIVE_ORDERING)
}

export type NavItem<T = Session> =
  | { kind: 'header'; key: string; label: string; count: number; collapsed: boolean }
  | { kind: 'session'; session: T }

export function buildNavItems<T>(
  groups: readonly SessionGroup<T>[],
  collapsedKeys: ReadonlySet<string>,
): NavItem<T>[] {
  const items: NavItem<T>[] = []
  for (const g of groups) {
    const collapsed = collapsedKeys.has(g.key)
    if (g.label !== null) {
      items.push({ kind: 'header', key: g.key, label: g.label, count: g.count, collapsed })
    }
    if (!collapsed) {
      for (const s of g.sessions) items.push({ kind: 'session', session: s })
    }
  }
  return items
}
```

- [ ] **Step 4: Run the new + existing ordering/nav tests, verify all pass**

```bash
cd web && npx vitest run src/session/__tests__/ordering.test.ts src/session/__tests__/ordering.generic.test.ts src/session/__tests__/buildNavItems.test.ts && npx tsc -b --force
```
Expected: PASS. (The existing `ordering.test.ts` and `buildNavItems.test.ts` assert live grouping/sort output and `{kind:'session'}` — they validate behaviour is unchanged.)

- [ ] **Step 5: Commit**

```bash
git add web/src/session/ordering.ts web/src/session/__tests__/ordering.generic.test.ts
git commit -m "refactor(ordering): config-driven generic engine, live as wrapper

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Frontend — `repoFromCwd` helper

**Files:**
- Create: `web/src/lib/repoFromCwd.ts`
- Test: `web/src/lib/__tests__/repoFromCwd.test.ts` (new)

**Interfaces:**
- Produces: `function repoFromCwd(cwd: string | null | undefined): string | null` — best-effort repo name from a working-directory path; `null` when nothing usable. Designed to be fed into `formatRepoLabel` (which renders `null`/empty as the dimmed "Scratch" bucket).

- [ ] **Step 1: Write failing test** (`web/src/lib/__tests__/repoFromCwd.test.ts`)

```ts
import { describe, it, expect } from 'vitest'
import { repoFromCwd } from '../repoFromCwd'

describe('repoFromCwd', () => {
  it('uses the repo dir above a ccswitch worktree path', () => {
    expect(repoFromCwd('/home/u/GitHub/cc-account-switcher/.ccswitch/worktrees/cc-account-switcher/19bc'))
      .toBe('cc-account-switcher')
  })
  it('falls back to the basename for a plain repo dir', () => {
    expect(repoFromCwd('/home/u/GitHub/KinDash')).toBe('KinDash')
  })
  it('ignores a trailing slash', () => {
    expect(repoFromCwd('/home/u/GitHub/KinDash/')).toBe('KinDash')
  })
  it('returns null for empty / nullish', () => {
    expect(repoFromCwd('')).toBeNull()
    expect(repoFromCwd(null)).toBeNull()
    expect(repoFromCwd(undefined)).toBeNull()
  })
})
```

- [ ] **Step 2: Run, verify fail**

```bash
cd web && npx vitest run src/lib/__tests__/repoFromCwd.test.ts
```
Expected: FAIL — module not found.

- [ ] **Step 3: Implement** (`web/src/lib/repoFromCwd.ts`)

```ts
/**
 * Best-effort "which repo is this?" label from a working-directory path, for the
 * archived list (archived rows carry `cwd`, not the live `repo` field). Feed the
 * result into formatRepoLabel(), which renders null/empty as the "Scratch" bucket.
 *
 * ccswitch worktrees live at `<repo>/.ccswitch/worktrees/<name>/<hash>`, so the
 * basename is a meaningless hash — we take the segment just above `.ccswitch`
 * instead. Everything else falls back to the path basename.
 */
export function repoFromCwd(cwd: string | null | undefined): string | null {
  const trimmed = cwd?.trim()
  if (!trimmed) return null
  const parts = trimmed.split('/').filter(Boolean)
  if (parts.length === 0) return null
  const ccIdx = parts.indexOf('.ccswitch')
  if (ccIdx > 0) return parts[ccIdx - 1]
  return parts[parts.length - 1]
}
```

- [ ] **Step 4: Run, verify pass**

```bash
cd web && npx vitest run src/lib/__tests__/repoFromCwd.test.ts && npx tsc -b --force
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/repoFromCwd.ts web/src/lib/__tests__/repoFromCwd.test.ts
git commit -m "feat(lib): repoFromCwd — derive repo label from a cwd path

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Frontend — archived ordering config

**Files:**
- Create: `web/src/session/archivedOrdering.ts`
- Test: `web/src/session/__tests__/archivedOrdering.test.ts` (new)

**Interfaces:**
- Consumes: `OrderingConfig`, `orderItems` (`./ordering`), `formatRepoLabel` (`@/lib/repoLabel`), `repoFromCwd` (`@/lib/repoFromCwd`), `ArchivedSession` (`@/api/types`).
- Produces:
  - `type ArchGroupBy = 'none' | 'account' | 'repo'`
  - `type ArchSortBy = 'archived' | 'created' | 'name'`
  - `const ARCHIVED_ORDERING: OrderingConfig<ArchivedSession>`
  - `const ARCHIVED_GROUP_OPTIONS: { value: ArchGroupBy; label: string }[]`
  - `const ARCHIVED_SORT_OPTIONS: { value: ArchSortBy; label: string }[]`
  - `const ARCHIVED_DEFAULT_PREF: { groupBy: ArchGroupBy; sortBy: ArchSortBy }` = `{ groupBy: 'none', sortBy: 'archived' }`

- [ ] **Step 1: Write failing test** (`web/src/session/__tests__/archivedOrdering.test.ts`)

```ts
import { describe, it, expect } from 'vitest'
import { orderItems } from '../ordering'
import { ARCHIVED_ORDERING } from '../archivedOrdering'
import type { ArchivedSession } from '@/api/types'

const a = (over: Partial<ArchivedSession>): ArchivedSession => ({
  id: 'x', name: 'x', account: 'tony', cwd: '/home/u/GitHub/KinDash',
  claudeSessionId: null, permissionMode: 'default',
  createdAt: '2026-06-01T00:00:00Z', archivedAt: '2026-06-10T00:00:00Z', ...over,
})

describe('ARCHIVED_ORDERING', () => {
  it('sorts by archived date, newest first', () => {
    const rows = [
      a({ id: 'old', archivedAt: '2026-06-01T00:00:00Z' }),
      a({ id: 'new', archivedAt: '2026-06-20T00:00:00Z' }),
    ]
    const g = orderItems(rows, 'none', 'archived', ARCHIVED_ORDERING)
    expect(g[0].sessions.map((s) => s.id)).toEqual(['new', 'old'])
  })

  it('groups by account', () => {
    const rows = [a({ id: '1', account: 'tony' }), a({ id: '2', account: 'work' }), a({ id: '3', account: 'tony' })]
    const g = orderItems(rows, 'account', 'name', ARCHIVED_ORDERING)
    expect(g.map((x) => x.key)).toEqual(['tony', 'work'])
    expect(g[0].sessions.map((s) => s.id)).toEqual(['1', '3'])
  })

  it('groups by repo derived from cwd, scratch last', () => {
    const rows = [
      a({ id: 'k', cwd: '/home/u/GitHub/KinDash' }),
      a({ id: 's', cwd: '' }),
    ]
    const g = orderItems(rows, 'repo', 'name', ARCHIVED_ORDERING)
    expect(g.map((x) => x.label)).toEqual(['KinDash', 'Scratch'])
  })
})
```

- [ ] **Step 2: Run, verify fail**

```bash
cd web && npx vitest run src/session/__tests__/archivedOrdering.test.ts
```
Expected: FAIL — module not found.

- [ ] **Step 3: Implement** (`web/src/session/archivedOrdering.ts`)

```ts
import type { ArchivedSession } from '@/api/types'
import { formatRepoLabel } from '@/lib/repoLabel'
import { repoFromCwd } from '@/lib/repoFromCwd'
import type { OrderingConfig } from './ordering'

export type ArchGroupBy = 'none' | 'account' | 'repo'
export type ArchSortBy = 'archived' | 'created' | 'name'

const ms = (iso: string): number => {
  const t = new Date(iso).getTime()
  return Number.isNaN(t) ? 0 : t
}
const byId = (a: ArchivedSession, b: ArchivedSession): number =>
  a.id < b.id ? -1 : a.id > b.id ? 1 : 0

export const ARCHIVED_ORDERING: OrderingConfig<ArchivedSession> = {
  idOf: (s) => s.id,
  groupings: {
    account: {
      bucketOf: (s) => ({ key: s.account, label: s.account }),
    },
    repo: {
      bucketOf: (s) => {
        const rl = formatRepoLabel(repoFromCwd(s.cwd))
        return { key: rl.text, label: rl.text, order: rl.isScratch ? 1 : 0 }
      },
    },
  },
  sorts: {
    archived: (a, b) => ms(b.archivedAt) - ms(a.archivedAt) || byId(a, b),
    created: (a, b) => ms(b.createdAt) - ms(a.createdAt) || byId(a, b),
    name: (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }) || byId(a, b),
  },
}

export const ARCHIVED_GROUP_OPTIONS: { value: ArchGroupBy; label: string }[] = [
  { value: 'none', label: 'None' },
  { value: 'account', label: 'Account' },
  { value: 'repo', label: 'Repo' },
]
export const ARCHIVED_SORT_OPTIONS: { value: ArchSortBy; label: string }[] = [
  { value: 'archived', label: 'Archived date' },
  { value: 'created', label: 'Created date' },
  { value: 'name', label: 'Name' },
]
export const ARCHIVED_DEFAULT_PREF: { groupBy: ArchGroupBy; sortBy: ArchSortBy } = {
  groupBy: 'none',
  sortBy: 'archived',
}
```

- [ ] **Step 4: Run, verify pass**

```bash
cd web && npx vitest run src/session/__tests__/archivedOrdering.test.ts && npx tsc -b --force
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/archivedOrdering.ts web/src/session/__tests__/archivedOrdering.test.ts
git commit -m "feat(ordering): archived ordering config (account/repo · archived/created/name)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Frontend — namespaced persistence

**Files:**
- Modify: `web/src/lib/sessionOrder.ts`
- Modify: `web/src/lib/groupCollapse.ts`
- Test: `web/src/lib/__tests__/orderPref.test.ts` (new)

**Interfaces:**
- Produces (sessionOrder.ts):
  - `function loadOrderPref<G extends string, S extends string>(ns: string, validGroup: readonly G[], validSort: readonly S[], def: { groupBy: G; sortBy: S }): { groupBy: G; sortBy: S }`
  - `function saveOrderPref<G extends string, S extends string>(ns: string, pref: { groupBy: G; sortBy: S }): void`
  - unchanged: `loadSessionOrder()`, `saveSessionOrder(pref)`, `SessionOrderPref`.
- Produces (groupCollapse.ts):
  - `function loadGroupCollapseNs(ns: string): Set<string>`
  - `function saveGroupCollapseNs(ns: string, keys: Set<string>): void`
  - unchanged: `loadGroupCollapse()`, `saveGroupCollapse(keys)`.
- Namespace `'live'` → existing keys `ccswitch.sessionOrder` / `ccswitch.groupCollapse`; any other ns → suffixed key.

- [ ] **Step 1: Write failing test** (`web/src/lib/__tests__/orderPref.test.ts`)

```ts
import { describe, it, expect, beforeEach } from 'vitest'
import { loadOrderPref, saveOrderPref, loadSessionOrder } from '../sessionOrder'
import { loadGroupCollapseNs, saveGroupCollapseNs, loadGroupCollapse } from '../groupCollapse'

beforeEach(() => window.localStorage.clear())

describe('namespaced order prefs', () => {
  it('live namespace uses the legacy key (back-compat)', () => {
    window.localStorage.setItem('ccswitch.sessionOrder', JSON.stringify({ groupBy: 'repo', sortBy: 'name' }))
    expect(loadSessionOrder()).toEqual({ groupBy: 'repo', sortBy: 'name' })
  })

  it('a custom namespace is isolated and validates its own enums', () => {
    const G = ['none', 'account', 'repo'] as const
    const S = ['archived', 'created', 'name'] as const
    const def = { groupBy: 'none' as const, sortBy: 'archived' as const }
    saveOrderPref('archived', { groupBy: 'account', sortBy: 'name' })
    expect(loadOrderPref('archived', G, S, def)).toEqual({ groupBy: 'account', sortBy: 'name' })
    // unknown enum value falls back to default
    window.localStorage.setItem('ccswitch.sessionOrder.archived', JSON.stringify({ groupBy: 'status', sortBy: 'zzz' }))
    expect(loadOrderPref('archived', G, S, def)).toEqual(def)
    // did not touch the live key
    expect(loadSessionOrder()).toEqual({ groupBy: 'none', sortBy: 'status' })
  })

  it('collapse keys are namespaced', () => {
    saveGroupCollapseNs('archived', new Set(['tony']))
    expect([...loadGroupCollapseNs('archived')]).toEqual(['tony'])
    expect([...loadGroupCollapse()]).toEqual([]) // live untouched
  })
})
```

- [ ] **Step 2: Run, verify fail**

```bash
cd web && npx vitest run src/lib/__tests__/orderPref.test.ts
```
Expected: FAIL — `loadOrderPref`/`loadGroupCollapseNs` not exported.

- [ ] **Step 3a: Implement** `web/src/lib/sessionOrder.ts` (replace whole file)

```ts
/**
 * Persists session-list group/sort selection, namespaced per list ('live',
 * 'archived', …). All access is try/catch-wrapped so a blocked localStorage
 * degrades to defaults. Namespace 'live' maps to the original key so existing
 * user prefs survive.
 */
import type { GroupBy, SortBy } from '@/session/ordering'

const keyFor = (ns: string): string =>
  ns === 'live' ? 'ccswitch.sessionOrder' : `ccswitch.sessionOrder.${ns}`

export function loadOrderPref<G extends string, S extends string>(
  ns: string,
  validGroup: readonly G[],
  validSort: readonly S[],
  def: { groupBy: G; sortBy: S },
): { groupBy: G; sortBy: S } {
  try {
    const raw = window.localStorage.getItem(keyFor(ns))
    if (!raw) return { ...def }
    const parsed = JSON.parse(raw) as Partial<{ groupBy: G; sortBy: S }>
    return {
      groupBy: validGroup.includes(parsed.groupBy as G) ? (parsed.groupBy as G) : def.groupBy,
      sortBy: validSort.includes(parsed.sortBy as S) ? (parsed.sortBy as S) : def.sortBy,
    }
  } catch {
    return { ...def }
  }
}

export function saveOrderPref<G extends string, S extends string>(
  ns: string,
  pref: { groupBy: G; sortBy: S },
): void {
  try {
    window.localStorage.setItem(keyFor(ns), JSON.stringify(pref))
  } catch {
    /* ignore — persistence is best-effort */
  }
}

// ── Live wrappers (unchanged public API) ─────────────────────────────────────
export interface SessionOrderPref {
  groupBy: GroupBy
  sortBy: SortBy
}
const LIVE_GROUP: GroupBy[] = ['none', 'repo', 'status']
const LIVE_SORT: SortBy[] = ['status', 'lastActive', 'age', 'name']
const LIVE_DEFAULT: SessionOrderPref = { groupBy: 'none', sortBy: 'status' }

export function loadSessionOrder(): SessionOrderPref {
  return loadOrderPref('live', LIVE_GROUP, LIVE_SORT, LIVE_DEFAULT)
}
export function saveSessionOrder(pref: SessionOrderPref): void {
  saveOrderPref('live', pref)
}
```

- [ ] **Step 3b: Implement** `web/src/lib/groupCollapse.ts` (replace whole file)

```ts
/**
 * Persists which session-list groups are collapsed, namespaced per list. Stores
 * the set of COLLAPSED group keys (empty/absent ⇒ all expanded). Namespace 'live'
 * maps to the original key so existing collapse state survives.
 */
const keyFor = (ns: string): string =>
  ns === 'live' ? 'ccswitch.groupCollapse' : `ccswitch.groupCollapse.${ns}`

export function loadGroupCollapseNs(ns: string): Set<string> {
  try {
    const raw = window.localStorage.getItem(keyFor(ns))
    if (!raw) return new Set()
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((k): k is string => typeof k === 'string'))
  } catch {
    return new Set()
  }
}

export function saveGroupCollapseNs(ns: string, keys: Set<string>): void {
  try {
    window.localStorage.setItem(keyFor(ns), JSON.stringify([...keys]))
  } catch {
    /* ignore — persistence is best-effort */
  }
}

// ── Live wrappers (unchanged public API) ─────────────────────────────────────
export function loadGroupCollapse(): Set<string> {
  return loadGroupCollapseNs('live')
}
export function saveGroupCollapse(keys: Set<string>): void {
  saveGroupCollapseNs('live', keys)
}
```

- [ ] **Step 4: Run new + existing persistence tests, verify pass**

```bash
cd web && npx vitest run src/lib/__tests__/orderPref.test.ts && npx tsc -b --force
```
Expected: PASS. (No dedicated existing test file for these helpers; `useSessionOrdering.test.ts` exercises them indirectly and runs in Task 6.)

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/sessionOrder.ts web/src/lib/groupCollapse.ts web/src/lib/__tests__/orderPref.test.ts
git commit -m "refactor(persist): namespace order/collapse prefs; live keeps legacy keys

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Frontend — generic ordering hook

**Files:**
- Modify: `web/src/session/useSessionOrdering.ts`
- Test: `web/src/session/__tests__/useItemOrdering.test.ts` (new)

**Interfaces:**
- Consumes: `orderItems`, `buildNavItems`, `SessionGroup`, `NavItem`, `OrderingConfig`, `GroupBy`, `SortBy`, `LIVE_ORDERING` (`./ordering`); `loadOrderPref`/`saveOrderPref` (`@/lib/sessionOrder`); `loadGroupCollapseNs`/`saveGroupCollapseNs` (`@/lib/groupCollapse`).
- Produces:
  - `interface OrderingSpec<G extends string, S extends string, T> { namespace: string; config: OrderingConfig<T>; groupByValues: readonly G[]; sortByValues: readonly S[]; defaultPref: { groupBy: G; sortBy: S } }`
  - `interface OrderingHandle<G extends string, S extends string, T> { groupBy: G; sortBy: S; setGroupBy: (g: G) => void; setSortBy: (s: S) => void; groups: SessionGroup<T>[]; orderedSessions: T[]; navItems: NavItem<T>[]; collapsedKeys: Set<string>; toggleCollapse: (k: string) => void; expandGroup: (k: string) => void; collapseGroup: (k: string) => void }`
  - `function useItemOrdering<G, S, T>(items: readonly T[], spec: OrderingSpec<G, S, T>, matchedIds?: Set<string> | null): OrderingHandle<G, S, T>`
  - `const LIVE_SPEC: OrderingSpec<GroupBy, SortBy, Session>`
  - unchanged: `useSessionOrdering(sessions, matchedIds?)`, `SessionOrdering` (now `= OrderingHandle<GroupBy, SortBy, Session>`).

- [ ] **Step 1: Write failing test** (`web/src/session/__tests__/useItemOrdering.test.ts`)

```ts
import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useItemOrdering, type OrderingSpec } from '../useSessionOrdering'
import type { OrderingConfig } from '../ordering'

interface Row { id: string; acct: string }
const CONFIG: OrderingConfig<Row> = {
  idOf: (r) => r.id,
  groupings: { acct: { bucketOf: (r) => ({ key: r.acct, label: r.acct }) } },
  sorts: { id: (a, b) => a.id.localeCompare(b.id) },
}
const SPEC: OrderingSpec<'none' | 'acct', 'id', Row> = {
  namespace: 'test', config: CONFIG,
  groupByValues: ['none', 'acct'], sortByValues: ['id'],
  defaultPref: { groupBy: 'none', sortBy: 'id' },
}
const rows: Row[] = [{ id: 'b', acct: 'y' }, { id: 'a', acct: 'x' }]

beforeEach(() => window.localStorage.clear())

describe('useItemOrdering', () => {
  it('orders flat by default and exposes navItems', () => {
    const { result } = renderHook(() => useItemOrdering(rows, SPEC))
    expect(result.current.orderedSessions.map((r) => r.id)).toEqual(['a', 'b'])
  })

  it('regroups and filters by matchedIds', () => {
    const { result } = renderHook(() => useItemOrdering(rows, SPEC, new Set(['a'])))
    act(() => result.current.setGroupBy('acct'))
    expect(result.current.groups.map((g) => g.key)).toEqual(['x'])
  })
})
```

- [ ] **Step 2: Run, verify fail**

```bash
cd web && npx vitest run src/session/__tests__/useItemOrdering.test.ts
```
Expected: FAIL — `useItemOrdering` not exported.

- [ ] **Step 3: Implement** — rewrite `web/src/session/useSessionOrdering.ts`

```ts
import { useCallback, useEffect, useMemo, useState } from 'react'
import type { Session } from '@/api/types'
import {
  orderItems, buildNavItems, LIVE_ORDERING,
  type GroupBy, type SortBy, type SessionGroup, type NavItem, type OrderingConfig,
} from './ordering'
import { loadOrderPref, saveOrderPref } from '@/lib/sessionOrder'
import { loadGroupCollapseNs, saveGroupCollapseNs } from '@/lib/groupCollapse'

export interface OrderingSpec<G extends string, S extends string, T> {
  namespace: string
  config: OrderingConfig<T>
  groupByValues: readonly G[]
  sortByValues: readonly S[]
  defaultPref: { groupBy: G; sortBy: S }
}

export interface OrderingHandle<G extends string, S extends string, T> {
  groupBy: G
  sortBy: S
  setGroupBy: (g: G) => void
  setSortBy: (s: S) => void
  groups: SessionGroup<T>[]
  orderedSessions: T[]
  navItems: NavItem<T>[]
  collapsedKeys: Set<string>
  toggleCollapse: (key: string) => void
  expandGroup: (key: string) => void
  collapseGroup: (key: string) => void
}

/**
 * Generic group/sort/collapse state for a list, persisted under spec.namespace.
 * `matchedIds` (when non-null) filters to those item ids (search integration).
 */
export function useItemOrdering<G extends string, S extends string, T>(
  items: readonly T[],
  spec: OrderingSpec<G, S, T>,
  matchedIds: Set<string> | null = null,
): OrderingHandle<G, S, T> {
  const { namespace, config, groupByValues, sortByValues, defaultPref } = spec
  const [groupBy, setGroupBy] = useState<G>(
    () => loadOrderPref(namespace, groupByValues, sortByValues, defaultPref).groupBy,
  )
  const [sortBy, setSortBy] = useState<S>(
    () => loadOrderPref(namespace, groupByValues, sortByValues, defaultPref).sortBy,
  )
  useEffect(() => {
    saveOrderPref(namespace, { groupBy, sortBy })
  }, [namespace, groupBy, sortBy])

  const [collapsedKeys, setCollapsedKeys] = useState<Set<string>>(() => loadGroupCollapseNs(namespace))
  useEffect(() => {
    saveGroupCollapseNs(namespace, collapsedKeys)
  }, [namespace, collapsedKeys])

  const toggleCollapse = useCallback((key: string) => {
    setCollapsedKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])
  const expandGroup = useCallback((key: string) => {
    setCollapsedKeys((prev) => {
      if (!prev.has(key)) return prev
      const next = new Set(prev)
      next.delete(key)
      return next
    })
  }, [])
  const collapseGroup = useCallback((key: string) => {
    setCollapsedKeys((prev) => {
      if (prev.has(key)) return prev
      const next = new Set(prev)
      next.add(key)
      return next
    })
  }, [])

  const filtered = useMemo(
    () => (matchedIds ? items.filter((it) => matchedIds.has(config.idOf(it))) : items),
    [items, matchedIds, config],
  )
  const groups = useMemo(
    () => orderItems(filtered, groupBy, sortBy, config),
    [filtered, groupBy, sortBy, config],
  )
  const orderedSessions = useMemo(
    () => groups.filter((g) => !collapsedKeys.has(g.key)).flatMap((g) => g.sessions),
    [groups, collapsedKeys],
  )
  const navItems = useMemo(() => buildNavItems(groups, collapsedKeys), [groups, collapsedKeys])

  return {
    groupBy, sortBy, setGroupBy, setSortBy, groups, orderedSessions, navItems,
    collapsedKeys, toggleCollapse, expandGroup, collapseGroup,
  }
}

// ── Live wrapper (unchanged public API) ──────────────────────────────────────
export type SessionOrdering = OrderingHandle<GroupBy, SortBy, Session>

export const LIVE_SPEC: OrderingSpec<GroupBy, SortBy, Session> = {
  namespace: 'live',
  config: LIVE_ORDERING,
  groupByValues: ['none', 'repo', 'status'],
  sortByValues: ['status', 'lastActive', 'age', 'name'],
  defaultPref: { groupBy: 'none', sortBy: 'status' },
}

export function useSessionOrdering(
  sessions: readonly Session[],
  matchedIds: Set<string> | null = null,
): SessionOrdering {
  return useItemOrdering(sessions, LIVE_SPEC, matchedIds)
}
```

- [ ] **Step 4: Run new + existing hook tests, verify pass**

```bash
cd web && npx vitest run src/session/__tests__/useItemOrdering.test.ts src/session/__tests__/useSessionOrdering.test.ts && npx tsc -b --force
```
Expected: PASS — the existing `useSessionOrdering.test.ts` validates the live wrapper still behaves identically.

- [ ] **Step 5: Commit**

```bash
git add web/src/session/useSessionOrdering.ts web/src/session/__tests__/useItemOrdering.test.ts
git commit -m "refactor(ordering): generic useItemOrdering; useSessionOrdering wraps it

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Frontend — search hook `scope`

**Files:**
- Modify: `web/src/api/hooks.ts:23` (`QK.sessionSearch`)
- Modify: `web/src/session/useSessionSearch.ts`
- Test: `web/src/session/__tests__/useSessionSearch.test.ts` (update existing)

**Interfaces:**
- Produces: `QK.sessionSearch: (q: string, scope?: string) => ['session-search', string, string]` (scope defaults to `'live'`); `useSessionSearch(scope?: 'live' | 'archived'): SessionSearch`.

- [ ] **Step 1: Update the existing test** — open `web/src/session/__tests__/useSessionSearch.test.ts`. Find the assertion(s) on the fetched URL and the query key; update to expect `&scope=live` on the default path. Add one archived case:

```ts
it('targets the archived scope when asked', async () => {
  // mirror the existing live test's setup, but: useSessionSearch('archived')
  // and assert the request URL contains '&scope=archived'.
})
```
(Keep the existing live tests; only adjust the literal URL/key they assert so it matches `?q=…&scope=live` and `['session-search','live', …]`.)

- [ ] **Step 2: Run, verify fail**

```bash
cd web && npx vitest run src/session/__tests__/useSessionSearch.test.ts
```
Expected: FAIL — current code emits no `scope`.

- [ ] **Step 3a: Implement** `web/src/api/hooks.ts:23`

```ts
  sessionSearch: (q: string, scope: string = 'live') => ['session-search', scope, q] as const,
```

- [ ] **Step 3b: Implement** `web/src/session/useSessionSearch.ts` — add the scope param:

```ts
export function useSessionSearch(scope: 'live' | 'archived' = 'live'): SessionSearch {
  const [query, setQuery] = useState('')
  const debounced = useDebouncedValue(query, 250).trim()
  const enabled = debounced !== ''

  const { data, isError } = useQuery({
    queryKey: QK.sessionSearch(debounced, scope),
    queryFn: () =>
      api.get<{ ids: string[] }>(
        `/sessions/search?q=${encodeURIComponent(debounced)}&scope=${scope}`,
      ),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: 2_000,
  })

  const matchedIds = enabled && data && !isError ? new Set(data.ids) : null
  return { query, setQuery, matchedIds, isFiltered: matchedIds !== null }
}
```

- [ ] **Step 4: Run, verify pass**

```bash
cd web && npx vitest run src/session/__tests__/useSessionSearch.test.ts && npx tsc -b --force
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/hooks.ts web/src/session/useSessionSearch.ts web/src/session/__tests__/useSessionSearch.test.ts
git commit -m "feat(search): scope param on useSessionSearch

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Frontend — generic `SessionListControls`

**Files:**
- Modify: `web/src/components/SessionListControls.tsx`
- Test: `web/src/components/__tests__/SessionListControls.test.tsx` (extend)

**Interfaces:**
- Produces: `SessionListControls<G extends string = GroupBy, S extends string = SortBy>` with new optional props `groupOptions?: { value: G; label: string }[]`, `sortOptions?: { value: S; label: string }[]`, `searchPlaceholder?: string`, `searchAriaLabel?: string`. `onGroupByChange: (g: G) => void`, `onSortByChange: (s: S) => void`, `groupBy: G`, `sortBy: S`. Defaults reproduce today's live options/labels.

- [ ] **Step 1: Add a failing test** (extend `SessionListControls.test.tsx`)

```ts
it('renders injected archived options', () => {
  render(
    <SessionListControls
      groupBy="account"
      sortBy="archived"
      onGroupByChange={() => {}}
      onSortByChange={() => {}}
      searchQuery=""
      onSearchChange={() => {}}
      groupOptions={[{ value: 'account', label: 'Account' }, { value: 'repo', label: 'Repo' }]}
      sortOptions={[{ value: 'archived', label: 'Archived date' }]}
    />,
  )
  expect(screen.getByRole('option', { name: 'Account' })).toBeInTheDocument()
  expect(screen.getByRole('option', { name: 'Archived date' })).toBeInTheDocument()
})
```

- [ ] **Step 2: Run, verify fail**

```bash
cd web && npx vitest run src/components/__tests__/SessionListControls.test.tsx
```
Expected: FAIL — options not injectable; types reject `groupBy="account"`.

- [ ] **Step 3: Implement** — rewrite `web/src/components/SessionListControls.tsx`

```tsx
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { GroupBy, SortBy } from '@/session/ordering'

export interface SelectOption<V extends string> {
  value: V
  label: string
}

interface SessionListControlsProps<G extends string, S extends string> {
  groupBy: G
  sortBy: S
  onGroupByChange: (g: G) => void
  onSortByChange: (s: S) => void
  searchQuery: string
  onSearchChange: (q: string) => void
  groupOptions?: SelectOption<G>[]
  sortOptions?: SelectOption<S>[]
  searchPlaceholder?: string
  searchAriaLabel?: string
}

const LIVE_GROUP_OPTIONS: SelectOption<GroupBy>[] = [
  { value: 'none', label: 'None' },
  { value: 'repo', label: 'Repo' },
  { value: 'status', label: 'Status' },
]
const LIVE_SORT_OPTIONS: SelectOption<SortBy>[] = [
  { value: 'status', label: 'Status tier' },
  { value: 'lastActive', label: 'Last active' },
  { value: 'age', label: 'Age' },
  { value: 'name', label: 'Name' },
]

const selectClass = cn(
  'text-xs rounded-md border border-gray-200 dark:border-gray-700',
  'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300',
  'px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
)

export function SessionListControls<G extends string = GroupBy, S extends string = SortBy>({
  groupBy,
  sortBy,
  onGroupByChange,
  onSortByChange,
  searchQuery,
  onSearchChange,
  groupOptions = LIVE_GROUP_OPTIONS as unknown as SelectOption<G>[],
  sortOptions = LIVE_SORT_OPTIONS as unknown as SelectOption<S>[],
  searchPlaceholder = 'Search sessions…',
  searchAriaLabel = 'Search sessions',
}: SessionListControlsProps<G, S>) {
  return (
    <div className="flex items-center gap-3 flex-wrap">
      <div className="relative">
        <input
          type="search"
          aria-label={searchAriaLabel}
          placeholder={searchPlaceholder}
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
          onChange={(e) => onGroupByChange(e.target.value as G)}
        >
          {groupOptions.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </label>
      <label className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
        <span>Sort by</span>
        <select
          aria-label="Sort by"
          className={selectClass}
          value={sortBy}
          onChange={(e) => onSortByChange(e.target.value as S)}
        >
          {sortOptions.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </label>
    </div>
  )
}
```

- [ ] **Step 4: Run controls tests, verify pass** (the live `DesktopShell`/`Dashboard` callers pass no option props → defaults → unchanged behaviour)

```bash
cd web && npx vitest run src/components/__tests__/SessionListControls.test.tsx && npx tsc -b --force
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/SessionListControls.tsx web/src/components/__tests__/SessionListControls.test.tsx
git commit -m "refactor(controls): injectable group/sort options; default to live

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Frontend — extract generic `GroupedList`

**Files:**
- Create: `web/src/components/GroupedList.tsx`
- Modify: `web/src/components/SessionList.tsx` (grouped branch delegates to `GroupedList`)
- Test: `web/src/components/__tests__/GroupedList.test.tsx` (new)

**Interfaces:**
- Consumes: `GroupHeader` (`./GroupHeader`), `SessionGroup` (`@/session/ordering`).
- Produces: `GroupedList<T>` rendering the group blocks (header + collapsible panel). Props:
  ```ts
  interface GroupedListProps<T> {
    groups: SessionGroup<T>[]
    renderRow: (item: T) => React.ReactNode   // must return a keyed element
    collapsedKeys?: Set<string>
    onToggleCollapse?: (key: string) => void
    selectedGroupKey?: string | null
    onExpand?: (key: string) => void
    onCollapse?: (key: string) => void
    listMode?: boolean
    onNavigate?: (dir: 'up' | 'down') => void
    onExit?: () => void
  }
  ```
  Renders nothing extra around the group list (caller owns the listbox container). Mirrors the existing `SessionList` grouped JSX exactly, including aria panel wiring and the `label === null` (ungrouped) case where the header is omitted.

- [ ] **Step 1: Write failing test** (`web/src/components/__tests__/GroupedList.test.tsx`)

```tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { GroupedList } from '../GroupedList'
import type { SessionGroup } from '@/session/ordering'

interface Row { id: string }
const groups: SessionGroup<Row>[] = [
  { key: 'x', label: 'X', count: 1, sessions: [{ id: 'a' }] },
  { key: 'y', label: 'Y', count: 1, sessions: [{ id: 'b' }] },
]

describe('GroupedList', () => {
  it('renders a header per labelled group and its rows', () => {
    render(<GroupedList groups={groups} renderRow={(r) => <div key={r.id}>row-{r.id}</div>} />)
    expect(screen.getByText('X')).toBeInTheDocument()
    expect(screen.getByText('row-a')).toBeInTheDocument()
    expect(screen.getByText('row-b')).toBeInTheDocument()
  })

  it('hides rows of a collapsed group', () => {
    render(
      <GroupedList
        groups={groups}
        renderRow={(r) => <div key={r.id}>row-{r.id}</div>}
        collapsedKeys={new Set(['x'])}
      />,
    )
    expect(screen.queryByText('row-a')).not.toBeInTheDocument()
    expect(screen.getByText('row-b')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run, verify fail**

```bash
cd web && npx vitest run src/components/__tests__/GroupedList.test.tsx
```
Expected: FAIL — module not found.

- [ ] **Step 3a: Implement** (`web/src/components/GroupedList.tsx`) — lift the grouped JSX out of `SessionList` (current lines 152-185) verbatim, parameterized by `renderRow`:

```tsx
import * as React from 'react'
import { GroupHeader } from './GroupHeader'
import type { SessionGroup } from '@/session/ordering'

interface GroupedListProps<T> {
  groups: SessionGroup<T>[]
  /** Must return a keyed element (key on the row itself). */
  renderRow: (item: T) => React.ReactNode
  collapsedKeys?: Set<string>
  onToggleCollapse?: (key: string) => void
  selectedGroupKey?: string | null
  onExpand?: (key: string) => void
  onCollapse?: (key: string) => void
  listMode?: boolean
  onNavigate?: (dir: 'up' | 'down') => void
  onExit?: () => void
}

export function GroupedList<T>({
  groups,
  renderRow,
  collapsedKeys,
  onToggleCollapse,
  selectedGroupKey,
  onExpand,
  onCollapse,
  listMode,
  onNavigate,
  onExit,
}: GroupedListProps<T>) {
  return (
    <>
      {groups.map((group) => {
        const collapsed = collapsedKeys?.has(group.key) ?? false
        const panelId = `group-panel-${group.key}`
        return (
          <div key={group.key} className="space-y-2">
            {group.label !== null && (
              <GroupHeader
                groupKey={group.key}
                label={group.label}
                count={group.count}
                collapsed={collapsed}
                selected={group.key === selectedGroupKey}
                listMode={listMode}
                onToggle={(k) => onToggleCollapse?.(k)}
                onExpand={onExpand}
                onCollapse={onCollapse}
                onNavigate={onNavigate}
                onExit={onExit}
              />
            )}
            {!collapsed && (
              <div
                id={panelId}
                className="space-y-2"
                {...(group.label !== null
                  ? { role: 'group', 'aria-labelledby': `group-header-${group.key}` }
                  : {})}
              >
                {group.sessions.map(renderRow)}
              </div>
            )}
          </div>
        )
      })}
    </>
  )
}
```

- [ ] **Step 3b: Refactor** `web/src/components/SessionList.tsx` — replace the grouped branch (the `groups ? groups.map(...) : sessions.map(renderCard)` expression at lines 151-186) with a delegation to `GroupedList`, keeping the outer listbox `<div>` and the ungrouped branch:

```tsx
      {groups ? (
        <GroupedList
          groups={groups}
          renderRow={renderCard}
          collapsedKeys={collapsedKeys}
          onToggleCollapse={onToggleCollapse}
          selectedGroupKey={selectedGroupKey}
          onExpand={onExpand}
          onCollapse={onCollapse}
          listMode={listMode}
          onNavigate={onNavigate}
          onExit={onExit}
        />
      ) : (
        sessions.map(renderCard)
      )}
```
Add `import { GroupedList } from './GroupedList'` and remove the now-unused `import { GroupHeader }` from `SessionList.tsx` (GroupHeader is used only inside GroupedList now). `renderCard` already returns a keyed `<SessionCard>` — unchanged.

- [ ] **Step 4: Run GroupedList + SessionList tests, verify pass**

```bash
cd web && npx vitest run src/components/__tests__/GroupedList.test.tsx src/components/__tests__/SessionList.test.tsx && npx tsc -b --force && npx eslint src/components/GroupedList.tsx src/components/SessionList.tsx
```
Expected: PASS (existing `SessionList.test.tsx` validates the grouped rendering is unchanged after delegation).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/GroupedList.tsx web/src/components/SessionList.tsx web/src/components/__tests__/GroupedList.test.tsx
git commit -m "refactor(list): extract generic GroupedList from SessionList

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 10: Frontend — grouping/sorting/search in `ArchivedManager`

**Files:**
- Modify: `web/src/components/ArchivedManager.tsx`
- Test: `web/src/components/__tests__/ArchivedManager.test.tsx` (new)

**Interfaces:**
- Consumes: `useSessionSearch` (scope `'archived'`), `useItemOrdering`, `ARCHIVED_ORDERING`/`ARCHIVED_GROUP_OPTIONS`/`ARCHIVED_SORT_OPTIONS`/`ARCHIVED_DEFAULT_PREF`/`ArchGroupBy`/`ArchSortBy` (`@/session/archivedOrdering`), `SessionListControls`, `GroupedList`, existing archive hooks.
- Produces: `ARCHIVED_SPEC` (module const inside ArchivedManager or archivedOrdering — put it in `archivedOrdering.ts` for reuse): `OrderingSpec<ArchGroupBy, ArchSortBy, ArchivedSession>` = `{ namespace: 'archived', config: ARCHIVED_ORDERING, groupByValues: ['none','account','repo'], sortByValues: ['archived','created','name'], defaultPref: ARCHIVED_DEFAULT_PREF }`. `ArchivedManager` props unchanged: `{ onReviewSession: (id: string) => void }`.

- [ ] **Step 1: Add `ARCHIVED_SPEC`** to `web/src/session/archivedOrdering.ts` (so both the component and tests import it):

```ts
import type { OrderingSpec } from './useSessionOrdering'
// …after the option consts…
export const ARCHIVED_SPEC: OrderingSpec<ArchGroupBy, ArchSortBy, ArchivedSession> = {
  namespace: 'archived',
  config: ARCHIVED_ORDERING,
  groupByValues: ['none', 'account', 'repo'],
  sortByValues: ['archived', 'created', 'name'],
  defaultPref: ARCHIVED_DEFAULT_PREF,
}
```
(If this introduces an import cycle `archivedOrdering → useSessionOrdering → ordering`, it won't — `useSessionOrdering` does not import `archivedOrdering`. Keep the type-only import `import type`.)

- [ ] **Step 2: Write failing test** (`web/src/components/__tests__/ArchivedManager.test.tsx`) — model it on existing archived/Dashboard tests for the query-client wrapper; mock `useArchivedSessions` to return two rows in different accounts and assert the controls render and grouping/search work. Use the project's standard test wrapper (copy the `renderWithClient`/`QueryClientProvider` setup from `ArchivedTab.test.tsx`).

```tsx
// Pseudocode shape — match the repo's existing wrapper/mocks:
it('renders archived controls and groups by account', async () => {
  mockArchived([
    { id: '1', name: 'Alpha', account: 'tony', cwd: '/h/GitHub/KinDash', archivedAt: '2026-06-10T00:00:00Z', createdAt: '2026-06-01T00:00:00Z', claudeSessionId: null, permissionMode: 'default' },
    { id: '2', name: 'Beta', account: 'work', cwd: '/h/GitHub/infra', archivedAt: '2026-06-12T00:00:00Z', createdAt: '2026-06-02T00:00:00Z', claudeSessionId: null, permissionMode: 'default' },
  ])
  render(<ArchivedManager onReviewSession={() => {}} />, { wrapper })
  expect(screen.getByLabelText('Group by')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Group by'), { target: { value: 'account' } })
  expect(screen.getByText('tony')).toBeInTheDocument()
  expect(screen.getByText('work')).toBeInTheDocument()
})

it('search filters the archived list', async () => {
  // useSessionSearch hits /sessions/search?...&scope=archived; mock api.get to
  // return { ids: ['1'] } for a non-empty query and assert only 'Alpha' shows.
})
```

- [ ] **Step 3: Run, verify fail**

```bash
cd web && npx vitest run src/components/__tests__/ArchivedManager.test.tsx
```
Expected: FAIL — no controls rendered yet.

- [ ] **Step 4: Implement** — wire into `ArchivedManager` (`web/src/components/ArchivedManager.tsx`). Changes:
  1. Add imports: `SessionListControls`, `GroupedList`, `useSessionSearch`, `useItemOrdering`, and from `@/session/archivedOrdering`: `ARCHIVED_SPEC`, `ARCHIVED_GROUP_OPTIONS`, `ARCHIVED_SORT_OPTIONS`.
  2. In the component body, after the hooks:
     ```tsx
     const search = useSessionSearch('archived')
     const ordering = useItemOrdering(archivedSessions, ARCHIVED_SPEC, search.matchedIds)
     const visible = ordering.orderedSessions   // filtered + sorted, collapsed-aware
     ```
  3. Make select-all / "all selected" operate on `visible` (the filtered set), not the raw `archivedSessions`. Update `toggleAll` and the `allSelected`/`someSelected` derivations in `ArchivedList` accordingly — pass `visible` down as the list’s `sessions`.
  4. Render a controls strip above the list (only when there is ≥1 archived session):
     ```tsx
     {archivedSessions.length > 0 && (
       <SessionListControls
         groupBy={ordering.groupBy}
         sortBy={ordering.sortBy}
         onGroupByChange={ordering.setGroupBy}
         onSortByChange={ordering.setSortBy}
         groupOptions={ARCHIVED_GROUP_OPTIONS}
         sortOptions={ARCHIVED_SORT_OPTIONS}
         searchQuery={search.query}
         onSearchChange={search.setQuery}
         searchPlaceholder="Search archived…"
         searchAriaLabel="Search archived sessions"
       />
     )}
     ```
  5. Replace the flat `<ul>` body of `ArchivedList` with grouped rendering via `GroupedList`, keeping the existing per-row markup but converting each row from `<li>` to a keyed `<div>` (a `<div role="group">` panel can't hold bare `<li>`s). Extract the existing row JSX into `renderArchivedRow(s: ArchivedSession)` returning a keyed `<div>`; keep the checkbox + Review/Unarchive/Delete actions identical. Keep the "Select all" header row above `GroupedList`.
     ```tsx
     <GroupedList
       groups={ordering.groups}
       renderRow={renderArchivedRow}
       collapsedKeys={ordering.collapsedKeys}
       onToggleCollapse={ordering.toggleCollapse}
     />
     ```
  6. Empty states: if `archivedSessions.length === 0` → existing "No archived sessions." If `search.isFiltered && visible.length === 0` → "No archived sessions match your search." (with a Clear button calling `search.setQuery('')`).

- [ ] **Step 5: Run, verify pass + lint + types**

```bash
cd web && npx vitest run src/components/__tests__/ArchivedManager.test.tsx && npx tsc -b --force && npx eslint src/components/ArchivedManager.tsx src/session/archivedOrdering.ts
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/ArchivedManager.tsx web/src/session/archivedOrdering.ts web/src/components/__tests__/ArchivedManager.test.tsx
git commit -m "feat(archived): grouping, sorting & search in ArchivedManager

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 11: Frontend — collapse the archived dual implementation (mobile)

**Files:**
- Modify: `web/src/components/Dashboard.tsx` — render `<ArchivedManager/>` in the archived tab; delete the inline `ArchivedList`, `BulkActionBar`, `BulkPermanentDeleteDialog`, `archivedDisplayName`, and the archived bulk-selection state/handlers (approx. lines 416-696 and the archived state around 767-1005, plus the archived-tab panel JSX).
- Test: existing `web/src/components/__tests__/ArchivedTab.test.tsx`, `DashboardTabs.test.tsx` (run; update only if they asserted on inline markup).

**Interfaces:**
- Consumes: `ArchivedManager` (already imported in DesktopShell; add the import to Dashboard). Precedent: Dashboard already renders the shared `AccountsManager` rather than a private copy (#222).

- [ ] **Step 1: Confirm the seam** — open `web/src/components/Dashboard.tsx`, locate the archived `tabpanel-archived` panel JSX and the inline archived components/state (grep `archived` — see lines ~416-696, ~767-1005, ~1080+). Identify the `onReviewSession` callback the inline list used so the shared component gets the same wiring.

- [ ] **Step 2: Replace the panel** — in the archived tab panel, render:
  ```tsx
  <div id="tabpanel-archived" role="tabpanel" aria-labelledby="tab-archived" hidden={activeTab !== 'archived'}>
    {activeTab === 'archived' && <ArchivedManager onReviewSession={handleReviewArchived} />}
  </div>
  ```
  (Use the existing review handler name; `ArchivedManager` owns its own data/bulk/search/group state now.) Add `import { ArchivedManager } from './ArchivedManager'`.

- [ ] **Step 3: Delete the dead inline code** — remove the now-unused inline `ArchivedList`, `BulkActionBar`, `BulkPermanentDeleteDialog`, `archivedDisplayName`, `summarizeBatch` (if only used by archived), and the archived bulk-selection `useState`/handlers (`archivedSelected`, `setArchivedSelected`, `toggleArchivedAll`, `archivedSummary`, bulk unarchive/delete handlers, `archivedSelectedNames`, the render-time pruning block). Remove now-unused imports (`useArchivedSessions`, `useUnarchiveSession`, `usePermanentDeleteSession`, `useBulkUnarchiveSessions`, `useBulkPermanentDeleteSessions`, `Trash2`, `ArchivedSession`, `BatchResult`) **only if no other part of Dashboard uses them** — verify each with a grep before deleting.

- [ ] **Step 4: Run the archived + dashboard test suites, verify pass**

```bash
cd web && npx vitest run src/components/__tests__/ArchivedTab.test.tsx src/components/__tests__/DashboardTabs.test.tsx src/components/__tests__/ArchivedManager.test.tsx && npx tsc -b --force && npx eslint src/components/Dashboard.tsx
```
Expected: PASS. If `ArchivedTab.test.tsx` asserted on inline-only markup that moved into `ArchivedManager`, update those assertions to match the shared component (it renders the same actions).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/Dashboard.tsx web/src/components/__tests__/
git commit -m "refactor(archived): Dashboard renders shared ArchivedManager (kill dual impl)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 12: Full verification + finish

- [ ] **Step 1: Full web suite + gates**

```bash
cd web && npx vitest run && npx tsc -b --force && npx eslint .
```
Expected: all green, eslint 0 warnings.

- [ ] **Step 2: Full backend suite + gates**

```bash
cd "$WT" && PYTHONPATH="$WT/src" "$MAIN/.venv/bin/python" -m ruff check server && PYTHONPATH="$WT/src" "$MAIN/.venv/bin/python" -m mypy server && PYTHONPATH="$WT/src" "$MAIN/.venv/bin/python" -m pytest server/tests/ -q
```
Expected: all green. (Two known environmental failures may appear per CLAUDE.md notes — `test_remote_mode_wiring`/x11; confirm they are pre-existing and unrelated.)

- [ ] **Step 3: Manual smoke (optional but recommended)** — run the app, open the archived tab on both a wide and narrow viewport; verify the group-by/sort/search controls appear, grouping by account/repo works, sorting by archived/created/name works, and search filters by transcript text. Confirm the live list is unchanged.

- [ ] **Step 4: Push + PR** — per the project completion workflow:

```bash
git push -u origin feat/archived-list-grouping-sorting-search
gh pr create --title "feat(archived): grouping, sorting & searching for the archived list" \
  --body "Implements docs/superpowers/specs/2026-06-24-archived-list-grouping-sorting-searching-design.md (Approach C): a config-driven ordering/search engine reused by both the live and archived lists, plus backend \`scope=archived\` search. Collapses the archived mobile/desktop dual implementation.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
gh pr merge <#> --squash --auto
```

---

## Self-Review

**Spec coverage:**
- Backend `scope=live|archived`, reuse pure helpers, 422 on bad scope → Task 1. ✓
- Generic ordering engine (config-driven) → Task 2. ✓
- Archived dimensions (account/repo · archived/created/name) → Task 4. ✓
- `repo` derived from `cwd` via one shared helper feeding `formatRepoLabel` → Task 3 + used in Task 4. ✓
- Namespaced persistence, live keys frozen → Task 5. ✓
- Generic hook + search scope → Tasks 6, 7. ✓
- Shared controls + list shell → Tasks 8, 9. ✓
- Archived list gains the capability → Task 10. ✓
- Collapse the archived dual implementation → Task 11. ✓
- Testing (backend scope cases; FE ordering configs; component render) → Tasks 1, 2, 4, 10. ✓
- Out-of-scope items (status axis, saved searches, pagination, DB backfill) — none introduced. ✓

**Placeholder scan:** Task 10 step 2 and Task 11 use "match the repo's existing wrapper/mocks" rather than literal test code, because the exact query-client test harness must be copied from the neighbouring `ArchivedTab.test.tsx` to stay consistent — the implementer is directed to that file. All engine/library code is fully spelled out.

**Type consistency:** `SessionGroup<T=Session>.sessions`, `NavItem<T=Session>.session`, `OrderingConfig<T>.idOf/groupings/sorts`, `orderItems(items,groupBy,sortBy,config)`, `OrderingSpec`/`OrderingHandle`/`useItemOrdering`, `ARCHIVED_SPEC`, `SelectOption<V>` and the `SessionListControls<G,S>` generics, and `QK.sessionSearch(q,scope)` are used consistently across tasks 2→4→6→8→10.
