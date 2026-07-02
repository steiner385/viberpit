# Poll↔WS Version-Guarded Merge (PR 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the 5s session-list poll from clobbering a fresher WebSocket status patch (and vice-versa), by reconciling both channels against a server-provided monotonic per-session ordering token.

**Architecture:** The server exposes a `revAt` token — the row-write `updated_at` time, bumped on every status persist, serialized via `iso_utc` through one `SessionRecord.rev_at` property and carried across the RPC boundary (`to_dict`/`from_dict`) so it survives remote mode. The client keeps, per session, whichever of the poll result and the WS patch carries the newer `revAt`: the WS patch applies unless strictly older (version guard), and the poll merges via TanStack Query's `structuralSharing`, keeping a cached session whose `revAt` is newer than the incoming poll's. Reconciliation logic lives in one pure, unit-tested module.

**Tech Stack:** Python (FastAPI/asyncio, pytest) backend · React + TypeScript (vitest, TanStack Query v5) frontend.

## Global Constraints

- Single source of truth (constitution VI): the token serialization (`iso_utc(updated_at)`) exists in exactly one place — a `SessionRecord.rev_at` property — used by `to_dict`, `_record_to_dict`, and the WS status publish. The reconciliation ordering logic exists in exactly one place — `web/src/session/reconcile.ts` — used by both the WS patch and the poll merge.
- The token is `revAt` (row-write `updated_at`), DISTINCT from `updatedAt` (freshness = `last_turn_at`, from PR 2). `revAt` bumps on EVERY status persist (so it can order two `in_progress`-ish updates that leave `last_turn_at` unchanged); `updatedAt` does not.
- Remote mode is the default: the token MUST survive `to_dict`→`from_dict` (edge reconstructs `SessionRecord` from the daemon's dict before `_record_to_dict`). A token that is only on `to_dict` but not restored in `from_dict` is invisible in prod.
- No new SQLite columns (the `updated_at` column already exists). No new orchestrator methods (so no RemoteOrchestrator proxy/RPC registration needed — this is field serialization only).
- Test env (fresh worktree): server via `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest …` from the worktree root; web in `web/` via `npx vitest run` / `npx tsc --noEmit` / `npx eslint` (`web/node_modules` symlinked). Do NOT run `uv sync` / `npm install`.

---

### Task 1: Server — `rev_at` token, bumped on persist, carried across RPC

**Files:**
- Modify: `server/sessions/types.py` — add `rev_at` property; emit `revAt` in `to_dict`; restore `updated_at` from `revAt` in `from_dict`.
- Modify: `server/api/sessions.py:131` area — add `"revAt": record.rev_at` to `_record_to_dict`.
- Modify: `server/sessions/orchestrator.py` — `_persist_status`: bump `rec.updated_at`; add `revAt` to the published status event.
- Test: `server/tests/test_rev_at_token.py` (create)

**Interfaces:**
- Produces: `SessionRecord.rev_at -> str | None` = `iso_utc(self.updated_at)`. The `/sessions` REST payload and the WS `status` frame gain `revAt: str | None`. `to_dict`↔`from_dict` round-trips `updated_at` via `revAt`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_rev_at_token.py
from datetime import datetime
from server.sessions.types import SessionRecord


def _rec(**kw) -> SessionRecord:
    base = dict(id="s1", name="n", account="a", claude_session_id=None,
                cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_rev_at_is_updated_at_iso():
    r = _rec(updated_at=datetime(2021, 6, 1, 12, 0, 0))
    assert r.rev_at == "2021-06-01T12:00:00+00:00"


def test_rev_at_none_when_no_updated_at():
    assert _rec(updated_at=None).rev_at is None


def test_to_dict_emits_revAt():
    r = _rec(updated_at=datetime(2021, 6, 1, 12, 0, 0))
    assert r.to_dict()["revAt"] == r.rev_at


def test_from_dict_restores_updated_at_via_revAt():
    r = _rec(updated_at=datetime(2021, 6, 1, 12, 0, 0),
             created_at=datetime(2020, 1, 1))
    back = SessionRecord.from_dict(r.to_dict())
    assert back.updated_at == r.updated_at   # token survives the RPC round-trip
    assert back.rev_at == r.rev_at
```

- [ ] **Step 2: Run to verify it fails**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_rev_at_token.py -v`
Expected: FAIL — `AttributeError: … 'rev_at'`.

- [ ] **Step 3: Add the `rev_at` property and emit it in `to_dict`**

In `server/sessions/types.py`, add just below the `updated_at_iso` property (added in PR 2):

```python
    @property
    def rev_at(self) -> str | None:
        """Monotonic per-session ordering token for client poll↔WS reconciliation: the
        row-write time (`updated_at`, bumped on EVERY status persist), serialized. Distinct
        from `updated_at_iso` (freshness = last_turn_at). The client keeps whichever of the
        poll and the live WS patch carries the newer `rev_at`, so a stale in-flight poll
        cannot overwrite a fresher live status."""
        return iso_utc(self.updated_at)
```

In `to_dict`'s returned dict, add after the `"lastTurnAt"` line:

```python
            "revAt": self.rev_at,
```

- [ ] **Step 4: Restore `updated_at` from `revAt` in `from_dict`**

In `server/sessions/types.py` `from_dict`, add near the other timestamp parsing (after `last_turn = d.get("lastTurnAt")`):

```python
        rev = d.get("revAt")
```

and add this kwarg to the `cls(...)` constructor call (e.g. right after `last_turn_at=...`):

```python
            updated_at=datetime.fromisoformat(rev) if rev else None,
```

(Replaces the old behavior of leaving `updated_at` unset over RPC — now the row-write token is explicitly and correctly carried as `revAt`.)

- [ ] **Step 5: Emit `revAt` from the HTTP serializer**

In `server/api/sessions.py` `_record_to_dict`, add after the `"updatedAt"` line (~131):

```python
        "revAt": record.rev_at,
```

- [ ] **Step 6: Bump `rec.updated_at` on every persist + publish `revAt`**

In `server/sessions/orchestrator.py` `_persist_status`, inside the `if rec is not None:` block (where `rec.status = status` is set, ~line 748-751), add an unconditional bump (the row-write clock advances on every status change, unlike `last_turn_at`):

```python
        if rec is not None:
            rec.status = status
            rec.updated_at = datetime.utcnow()   # row-write token; bumps on EVERY persist (see rev_at)
            if turn_at is not None:
                rec.last_turn_at = turn_at
```

Then extend the published status event (the line added in PR 2) to carry `revAt`:

```python
        self._publish_event({"type": "status", "sessionId": session_id, "status": status,
                             "updatedAt": rec.updated_at_iso if rec is not None else None,
                             "revAt": rec.rev_at if rec is not None else None})
```

- [ ] **Step 7: Run the new test + serialization/RPC/remote regressions**

Run: `/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_rev_at_token.py server/tests/ -k "to_dict or from_dict or record_to_dict or remote or rpc or persist_status or status_name or sessions_api or roundtrip or breadth" -q`
Expected: PASS. If any existing test asserts an EXACT `to_dict`/`from_dict`/`_record_to_dict` dict, update it to include the new `revAt` key (do not drop assertions — add the key). Note each such edit in the report.

- [ ] **Step 8: Commit**

```bash
git add server/sessions/types.py server/api/sessions.py server/sessions/orchestrator.py server/tests/test_rev_at_token.py
git commit -m "feat(freshness): server rev_at token bumped on every persist, carried across RPC

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Client — pure reconciliation helper

**Files:**
- Create: `web/src/session/reconcile.ts`
- Test: `web/src/session/reconcile.test.ts`

**Interfaces:**
- Consumes: `Session` (`web/src/api/types.ts`), which will gain `revAt?: string | null` in Task 3 — for this task, treat `revAt` as an optional string field.
- Produces:
  - `revIsNewer(candidate, existing): boolean` — strictly-newer-by-token; absent candidate → false; absent existing but real candidate → true; equal → false.
  - `shouldApplyRev(incoming, cached): boolean` — apply an incoming WS update unless it is strictly older than cached; missing either token → apply (no basis to reject).
  - `reconcileSessions(prev, next): Session[]` — merge a fresh poll (`next`) against the cache (`prev`), keeping a cached session whose token is newer than the poll's. `next` is authoritative for existence (a session absent from `next` is dropped).

- [ ] **Step 1: Write the failing tests**

```typescript
// web/src/session/reconcile.test.ts
import { describe, it, expect } from 'vitest'
import { revIsNewer, shouldApplyRev, reconcileSessions } from './reconcile'
import type { Session } from '@/api/types'

const s = (id: string, over: Partial<Session> = {}): Session =>
  ({ id, status: 'idle', ...over }) as Session
const T1 = '2021-06-01T12:00:00+00:00'
const T2 = '2021-06-01T12:00:05+00:00'  // 5s later

describe('revIsNewer', () => {
  it('true when candidate strictly newer', () => expect(revIsNewer(T2, T1)).toBe(true))
  it('false when equal', () => expect(revIsNewer(T1, T1)).toBe(false))
  it('false when candidate older', () => expect(revIsNewer(T1, T2)).toBe(false))
  it('false when candidate missing', () => expect(revIsNewer(undefined, T1)).toBe(false))
  it('true when existing missing but candidate present', () => expect(revIsNewer(T1, undefined)).toBe(true))
})

describe('shouldApplyRev', () => {
  it('applies newer', () => expect(shouldApplyRev(T2, T1)).toBe(true))
  it('applies equal', () => expect(shouldApplyRev(T1, T1)).toBe(true))
  it('rejects strictly older', () => expect(shouldApplyRev(T1, T2)).toBe(false))
  it('applies when a token is missing', () => expect(shouldApplyRev(undefined, T1)).toBe(true))
})

describe('reconcileSessions', () => {
  it('returns next as-is when no prev', () => {
    expect(reconcileSessions(undefined, [s('a', { revAt: T1 })])).toEqual([s('a', { revAt: T1 })])
  })
  it('keeps the cached session when its token is newer than the poll', () => {
    const prev = [s('a', { status: 'done', revAt: T2 })]
    const next = [s('a', { status: 'in_progress', revAt: T1 })]  // stale poll
    expect(reconcileSessions(prev, next)[0].status).toBe('done')
  })
  it('takes the poll when it is newer', () => {
    const prev = [s('a', { status: 'in_progress', revAt: T1 })]
    const next = [s('a', { status: 'done', revAt: T2 })]
    expect(reconcileSessions(prev, next)[0].status).toBe('done')
  })
  it('drops a session absent from the poll (poll is authoritative for existence)', () => {
    const prev = [s('a', { revAt: T2 }), s('b', { revAt: T2 })]
    const next = [s('a', { revAt: T1 })]
    const out = reconcileSessions(prev, next)
    expect(out.map((x) => x.id)).toEqual(['a'])
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run (in `web/`): `npx vitest run src/session/reconcile.test.ts`
Expected: FAIL — `Cannot find module './reconcile'`.

- [ ] **Step 3: Implement `reconcile.ts`**

```typescript
// web/src/session/reconcile.ts
import type { Session } from '@/api/types'

/** Parse a rev token (ISO-8601 UTC string) to ms; NaN when absent/invalid. */
function revMs(revAt: string | null | undefined): number {
  return revAt ? Date.parse(revAt) : NaN
}

/** Strictly-newer-by-token. Absent candidate → false; absent existing but real candidate → true. */
export function revIsNewer(candidate: string | null | undefined, existing: string | null | undefined): boolean {
  const c = revMs(candidate)
  if (Number.isNaN(c)) return false
  const e = revMs(existing)
  if (Number.isNaN(e)) return true
  return c > e
}

/** Apply an incoming WS update unless it is strictly older than the cached one. A missing
 *  token on either side means there is no basis to reject → apply. */
export function shouldApplyRev(incoming: string | null | undefined, cached: string | null | undefined): boolean {
  const i = revMs(incoming), c = revMs(cached)
  if (Number.isNaN(i) || Number.isNaN(c)) return true
  return i >= c
}

/**
 * Merge a fresh poll result (`next`) against the current cache (`prev`), keeping a cached
 * session whose rev token is newer than the poll's — i.e. a live WS patch that outran the
 * in-flight poll. `next` remains authoritative for existence: a session absent from it is
 * dropped. Intended as TanStack Query `structuralSharing` for the sessions query.
 */
export function reconcileSessions(prev: Session[] | undefined, next: Session[]): Session[] {
  if (!prev) return next
  const prevById = new Map(prev.map((s) => [s.id, s]))
  return next.map((fresh) => {
    const cached = prevById.get(fresh.id)
    return cached && revIsNewer(cached.revAt, fresh.revAt) ? cached : fresh
  })
}
```

- [ ] **Step 4: Run to verify pass + typecheck**

Run (in `web/`): `npx vitest run src/session/reconcile.test.ts && npx tsc --noEmit`
Expected: PASS. (`tsc` may report that `Session` has no `revAt` yet — if so, this task can add `revAt?: string | null` to the `Session` interface in `web/src/api/types.ts` as the minimal enabling change; Task 3 relies on it too.)

- [ ] **Step 5: Commit**

```bash
git add web/src/session/reconcile.ts web/src/session/reconcile.test.ts web/src/api/types.ts
git commit -m "feat(freshness): pure poll/WS reconciliation helper (rev-token ordering)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Client — wire the version guard into the WS patch and the poll

**Files:**
- Modify: `web/src/api/types.ts` — ensure `Session` has `revAt?: string | null` (if not added in Task 2).
- Modify: `web/src/ws/client.ts` — `WsStatusMsg` gains `revAt?`; `'status'` dispatch passes it; `_patchSessionStatus` version-guards via `shouldApplyRev` and stores `revAt`.
- Modify: `web/src/api/hooks.ts` — `useSessions` gets `structuralSharing: reconcileSessions`.
- Test: `web/src/ws/client.reconcile.test.ts` (create)

**Interfaces:**
- Consumes: `shouldApplyRev`, `reconcileSessions` (Task 2); server `status` frame now carries `revAt` (Task 1).
- Produces: WS status patches ignore an incoming `revAt` strictly older than the cached one; the sessions poll no longer overwrites a session the WS patched to a newer `revAt`.

- [ ] **Step 1: Write the failing test**

```typescript
// web/src/ws/client.reconcile.test.ts
import { describe, it, expect } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { SessionSocket } from './client'
import { QK } from '@/api/hooks'
import type { Session } from '@/api/types'

const T1 = '2021-06-01T12:00:00+00:00'
const T2 = '2021-06-01T12:00:05+00:00'
function seed(qc: QueryClient, over: Partial<Session>) {
  qc.setQueryData<Session[]>(QK.sessions, [{ id: 's1', status: 'idle', accountName: 'a', ...over } as Session])
}
function makeSocket() {
  const qc = new QueryClient()
  const s = new SessionSocket('/ws') as unknown as { setQueryClient: (q: QueryClient) => void; _handleInbound: (m: unknown) => void }
  s.setQueryClient(qc)
  return { s, qc }
}
const row = (qc: QueryClient) => (qc.getQueryData<Session[]>(QK.sessions) ?? [])[0]

describe('SessionSocket rev-guarded status patch', () => {
  it('applies a status whose revAt is newer', () => {
    const { s, qc } = makeSocket(); seed(qc, { revAt: T1 })
    s._handleInbound({ type: 'status', sessionId: 's1', status: 'done', revAt: T2 })
    expect(row(qc).status).toBe('done')
    expect(row(qc).revAt).toBe(T2)
  })
  it('ignores a status whose revAt is strictly older (stale/duplicate frame)', () => {
    const { s, qc } = makeSocket(); seed(qc, { status: 'done', revAt: T2 })
    s._handleInbound({ type: 'status', sessionId: 's1', status: 'in_progress', revAt: T1 })
    expect(row(qc).status).toBe('done')   // unchanged — older frame rejected
    expect(row(qc).revAt).toBe(T2)
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run (in `web/`): `npx vitest run src/ws/client.reconcile.test.ts`
Expected: FAIL — the current patch ignores `revAt` (applies unconditionally and never stores it).

- [ ] **Step 3: Ensure the `Session` type carries `revAt`**

In `web/src/api/types.ts`, in the `Session` interface (near `updatedAt?: string | null`), ensure present:

```typescript
  revAt?: string | null
```

- [ ] **Step 4: Version-guard `_patchSessionStatus`**

In `web/src/ws/client.ts`: import the helper near the other imports:

```typescript
import { shouldApplyRev } from '@/session/reconcile'
```

Add `revAt` to `WsStatusMsg` (the interface near line 18):

```typescript
export interface WsStatusMsg       { type: 'status';        sessionId: string; status: string; updatedAt?: string; revAt?: string }
```

Pass it through in the `'status'` dispatch:

```typescript
        this._patchSessionStatus(msg.sessionId, msg.status as Session['status'], msg.updatedAt, msg.revAt)
```

Rewrite `_patchSessionStatus` (as left by PR 2) to add the rev guard and store `revAt`:

```typescript
  private _patchSessionStatus(sessionId: string, status: Session['status'], updatedAt?: string, revAt?: string) {
    const qc = this._queryClient
    if (!qc) return
    qc.setQueryData<Session[]>(QK.sessions, (old) => {
      if (!old) return old
      return old.map((s) => {
        if (s.id !== sessionId) return s
        // Version guard: ignore a status frame strictly older than what's cached (a stale
        // or duplicate frame that raced a newer poll/patch). Missing tokens → apply.
        if (!shouldApplyRev(revAt, s.revAt)) return s
        return { ...s, status,
                 ...(updatedAt ? { updatedAt } : {}),
                 ...(revAt ? { revAt } : {}) }
      })
    })
  }
```

- [ ] **Step 5: Merge the poll against the cache via `structuralSharing`**

In `web/src/api/hooks.ts`: import the helper and wire it into `useSessions`:

```typescript
import { reconcileSessions } from '@/session/reconcile'
```
```typescript
  const query = useQuery<Session[]>({
    queryKey: QK.sessions,
    queryFn: () => api.get<Session[]>(isSessionsViewing() ? '/sessions?viewing=1' : '/sessions'),
    staleTime: 3_000,
    refetchInterval: 5_000,
    // Version-guarded merge: a stale in-flight poll must not overwrite a session the WS
    // patched to a newer revAt. reconcileSessions keeps the newer of cache vs poll per id.
    structuralSharing: (prev, next) => reconcileSessions(prev as Session[] | undefined, next as Session[]),
  })
```

- [ ] **Step 6: Run the test + full web gates**

Run (in `web/`): `npx vitest run src/ws/client.reconcile.test.ts && npx vitest run && npx tsc --noEmit && npx eslint src/`
Expected: PASS (2 new) + full suite green + clean.

- [ ] **Step 7: Commit**

```bash
git add web/src/api/types.ts web/src/ws/client.ts web/src/api/hooks.ts web/src/ws/client.reconcile.test.ts
git commit -m "fix(freshness): version-guard WS status patch + poll merge (rev token)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage (PR 3 slice of the design doc):**
- Server exposes a monotonic per-session ordering token on both REST and WS → Task 1 (`rev_at` / `revAt`). ✓
- Token = row-write `updated_at` (option a), bumped on every persist so it orders `in_progress` transitions → Task 1. ✓
- Token survives the RPC boundary (remote/prod) → Task 1 (`to_dict`+`from_dict` round-trip, tested). ✓
- Single source: one `rev_at` property (server), one `reconcile.ts` (client) → Tasks 1, 2. ✓
- WS patch version-guarded; poll kept as a safety net that can't clobber a newer WS patch → Task 3 (`shouldApplyRev` + `structuralSharing`/`reconcileSessions`). ✓
- Poll remains authoritative for existence → Task 2 (`reconcileSessions` drops sessions absent from `next`). ✓

**Placeholder scan:** none — every step has real code + exact commands.

**Type consistency:** `rev_at -> str | None` matches `iso_utc`; `revIsNewer`/`shouldApplyRev`/`reconcileSessions` signatures match their Task 3 call sites; `_patchSessionStatus(sessionId, status, updatedAt?, revAt?)` matches the `'status'` dispatch; `Session.revAt?: string | null` matches wire.

**Out of scope:** PR 1 (transcript gap-healing, merged) and PR 2 (freshness single-source, in flight). No schema change (reuses `updated_at`); no new orchestrator method (field serialization only), so no RemoteOrchestrator proxy/RPC registration. Same-instant `updated_at` collisions (two persists in one microsecond) resolve as equal tokens — `shouldApplyRev` applies on equal, `reconcileSessions` takes the poll on equal; both are safe for near-simultaneous writes.
