# Bulk Operations for Archived Sessions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users select multiple archived sessions and unarchive or permanently delete them in one action, with best-effort partial-failure reporting.

**Architecture:** Two new backend batch endpoints loop the *existing* single-ID orchestrator methods (`orch.unarchive` / `orch.permanent_delete`) — no new orchestrator/RPC/remote surface. The frontend adds selection state + a bulk action bar to `ArchivedManager`, and two new React Query mutation hooks that POST an ID list and invalidate the session caches.

**Tech Stack:** Python FastAPI + Pydantic v2 (backend); React + TypeScript + @tanstack/react-query + Tailwind (frontend); pytest + vitest/testing-library (tests).

## Global Constraints

- **Route ordering:** batch routes MUST be declared in `server/api/sessions.py` *before* the dynamic `/{session_id}` route (currently line 372) — otherwise `POST /batch/unarchive` matches `/{session_id}/unarchive` with `session_id="batch"`. Insert them right after the `/archived` route (line 369).
- **Best-effort semantics:** batch endpoints always return **200** with `{"succeeded": [...], "failed": [{"id", "error"}]}`. A missing/non-archived session is a `failed` entry, never an HTTP error. Only validation (empty/oversized list) returns 422.
- **Batch cap:** `ids` is non-empty, max **200** entries (Pydantic `Field(min_length=1, max_length=200)`).
- **Audit:** each *successful* op writes its existing audit record (`session.unarchive` / `session.permanent_delete`), one per session.
- **No DB changes.** Archived state stays `Session.status == "archived"`.
- **Running backend tests:** use the prebuilt venv (this worktree has no built env):
  `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest …`
- **Running web tooling:** `web/node_modules` is symlinked from a sibling worktree; run the local binaries (`web/node_modules/.bin/vitest`, `…/tsc`, `…/eslint`), not `npx`.
- **Mock-factory hazard:** `ArchivedManager` renders inside `Dashboard`. `web/src/components/__tests__/ArchivedTab.test.tsx` mocks `@/api/hooks` with a *complete* factory object. Any new hook the subtree calls MUST be added to that factory and to `setupDefaultMocks`, or ~9 tests break (tsc/eslint won't catch it — only vitest will).

---

### Task 1: Backend — `BatchSessionIdsRequest` model + `POST /batch/unarchive`

**Files:**
- Modify: `server/api/sessions.py` (import `Field`; add model + endpoint after line 369, before `/{session_id}`)
- Test: `server/tests/test_api.py` (append tests + two local helpers)

**Interfaces:**
- Produces: `POST /api/sessions/batch/unarchive`, body `{"ids": [str, ...]}`, returns `{"succeeded": [str], "failed": [{"id": str, "error": str}]}`.
- Consumes: existing `orch.unarchive(session_id) -> SessionRecord | None`, `record_audit(...)`.

- [ ] **Step 1: Add the test helpers + failing test for the all-success path**

Append to `server/tests/test_api.py` (reuses module-local `temp_env`, `_seed_slot`, `_make_usage_fn`, `make_usage`, `fake_sdk_factory`, `IDENTITY_HEADERS`):

```python
# ---------------------------------------------------------------------------
# Batch archived-session operations
# ---------------------------------------------------------------------------

from contextlib import contextmanager


@contextmanager
def _client_with_account(d):
    """Yield a TestClient (inside lifespan) with one seeded account 'account1'.

    Must be called with temp_env() active (d is the temp home dir).
    """
    _seed_slot("account1")
    usage_fn = _make_usage_fn({"account1": make_usage(session=10.0, weekly=20.0)})
    with patch("ccswitch.accounts.usage_for", side_effect=usage_fn), \
         patch("server.sessions.orchestrator.select_account", return_value="account1"), \
         patch("server.sessions.orchestrator.compose", return_value=d / "cfg" / "account1"), \
         patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
        (d / "cfg" / "account1").mkdir(parents=True, exist_ok=True)
        from server.sessions.orchestrator import SessionOrchestrator
        from server.app import create_app
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), usage_fn=usage_fn, poll_interval=9999.0)
        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            yield client


def _create_and_archive(client, name):
    """Create a session then archive it (DELETE); return its id."""
    r = client.post("/api/sessions", json={"name": name, "account": "account1"}, headers=IDENTITY_HEADERS)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    d = client.delete(f"/api/sessions/{sid}", headers=IDENTITY_HEADERS)
    assert d.status_code == 204, d.text
    return sid


def test_batch_unarchive_all_success():
    """POST /batch/unarchive restores every id; both reappear as active, archived list empties."""
    with temp_env() as d:
        with _client_with_account(d) as client:
            a = _create_and_archive(client, "A")
            b = _create_and_archive(client, "B")
            resp = client.post("/api/sessions/batch/unarchive", json={"ids": [a, b]}, headers=IDENTITY_HEADERS)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert set(body["succeeded"]) == {a, b}
            assert body["failed"] == []
            active_ids = [s["id"] for s in client.get("/api/sessions", headers=IDENTITY_HEADERS).json()]
            assert a in active_ids and b in active_ids
            assert client.get("/api/sessions/archived", headers=IDENTITY_HEADERS).json() == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_api.py::test_batch_unarchive_all_success -q`
Expected: FAIL — `404` (route not found) so `succeeded`/`failed` assertions error.

- [ ] **Step 3: Add the model + endpoint**

In `server/api/sessions.py`, change the pydantic import (line 21):

```python
from pydantic import BaseModel, Field
```

Insert this block immediately after the `list_archived_sessions` function (after line 369, before the `@router.get("/{session_id}")` at line 372):

```python
# ---------------------------------------------------------------------------
# Batch operations on archived sessions
#
# These static routes MUST be declared before the dynamic "/{session_id}"
# routes below, or "batch" is matched as a session_id.
# ---------------------------------------------------------------------------


class BatchSessionIdsRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1, max_length=200)


@router.post("/batch/unarchive")
async def batch_unarchive_sessions(
    body: BatchSessionIdsRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Unarchive multiple sessions. Best-effort: returns per-id succeeded/failed."""
    orch = _get_orchestrator(request)
    succeeded: list[str] = []
    failed: list[dict] = []
    for session_id in dict.fromkeys(body.ids):  # dedupe, preserve order
        try:
            record = await orch.unarchive(session_id)
        except Exception as exc:  # noqa: BLE001 — best-effort batch must not abort
            log.warning("batch unarchive failed for %s: %s", session_id, exc)
            failed.append({"id": session_id, "error": str(exc)})
            continue
        if record is None:
            failed.append({"id": session_id, "error": "not found"})
            continue
        succeeded.append(session_id)
        await record_audit(
            db=db,
            type="session.unarchive",
            identity=identity,
            detail={"session_id": session_id},
        )
    return {"succeeded": succeeded, "failed": failed}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_api.py::test_batch_unarchive_all_success -q`
Expected: PASS.

- [ ] **Step 5: Add partial-failure + validation + auth tests**

Append to `server/tests/test_api.py`:

```python
def test_batch_unarchive_partial_failure():
    """A non-existent id lands in failed; valid ids still succeed."""
    with temp_env() as d:
        with _client_with_account(d) as client:
            a = _create_and_archive(client, "A")
            resp = client.post("/api/sessions/batch/unarchive", json={"ids": [a, "ghost"]}, headers=IDENTITY_HEADERS)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["succeeded"] == [a]
            assert body["failed"] == [{"id": "ghost", "error": "not found"}]


def test_batch_unarchive_empty_ids_returns_422():
    with temp_env() as d:
        with _client_with_account(d) as client:
            resp = client.post("/api/sessions/batch/unarchive", json={"ids": []}, headers=IDENTITY_HEADERS)
            assert resp.status_code == 422


def test_batch_unarchive_oversized_ids_returns_422():
    with temp_env() as d:
        with _client_with_account(d) as client:
            ids = [f"x{i}" for i in range(201)]
            resp = client.post("/api/sessions/batch/unarchive", json={"ids": ids}, headers=IDENTITY_HEADERS)
            assert resp.status_code == 422


def test_batch_unarchive_requires_identity():
    with temp_env() as d:
        with _client_with_account(d) as client:
            resp = client.post("/api/sessions/batch/unarchive", json={"ids": ["x"]})
            assert resp.status_code == 401
```

- [ ] **Step 6: Run all Task-1 tests**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_api.py -k batch_unarchive -q`
Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add server/api/sessions.py server/tests/test_api.py
git commit -m "feat(api): batch unarchive endpoint for archived sessions"
```

---

### Task 2: Backend — `POST /batch/permanent-delete`

**Files:**
- Modify: `server/api/sessions.py` (add endpoint right after `batch_unarchive_sessions`, still before `/{session_id}`)
- Test: `server/tests/test_api.py` (append)

**Interfaces:**
- Produces: `POST /api/sessions/batch/permanent-delete`, same body/response shape as batch unarchive.
- Consumes: existing `orch.permanent_delete(session_id) -> bool`, `record_audit(...)`, and the `BatchSessionIdsRequest` model from Task 1.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_api.py`:

```python
def test_batch_permanent_delete_all_success():
    """POST /batch/permanent-delete removes every id; archived list empties."""
    with temp_env() as d:
        with _client_with_account(d) as client:
            a = _create_and_archive(client, "A")
            b = _create_and_archive(client, "B")
            resp = client.post("/api/sessions/batch/permanent-delete", json={"ids": [a, b]}, headers=IDENTITY_HEADERS)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert set(body["succeeded"]) == {a, b}
            assert body["failed"] == []
            assert client.get("/api/sessions/archived", headers=IDENTITY_HEADERS).json() == []


def test_batch_permanent_delete_partial_failure():
    with temp_env() as d:
        with _client_with_account(d) as client:
            a = _create_and_archive(client, "A")
            resp = client.post("/api/sessions/batch/permanent-delete", json={"ids": [a, "ghost"]}, headers=IDENTITY_HEADERS)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["succeeded"] == [a]
            assert body["failed"] == [{"id": "ghost", "error": "not found"}]
```

- [ ] **Step 2: Run to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_api.py -k batch_permanent_delete -q`
Expected: FAIL — 404 (route not found).

- [ ] **Step 3: Add the endpoint**

In `server/api/sessions.py`, immediately after `batch_unarchive_sessions` (and before `@router.get("/{session_id}")`):

```python
@router.post("/batch/permanent-delete")
async def batch_permanent_delete_sessions(
    body: BatchSessionIdsRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Permanently delete multiple sessions. Best-effort: returns per-id succeeded/failed."""
    orch = _get_orchestrator(request)
    succeeded: list[str] = []
    failed: list[dict] = []
    for session_id in dict.fromkeys(body.ids):  # dedupe, preserve order
        try:
            deleted = await orch.permanent_delete(session_id)
        except Exception as exc:  # noqa: BLE001 — best-effort batch must not abort
            log.warning("batch permanent-delete failed for %s: %s", session_id, exc)
            failed.append({"id": session_id, "error": str(exc)})
            continue
        if not deleted:
            failed.append({"id": session_id, "error": "not found"})
            continue
        succeeded.append(session_id)
        await record_audit(
            db=db,
            type="session.permanent_delete",
            identity=identity,
            detail={"session_id": session_id},
        )
    return {"succeeded": succeeded, "failed": failed}
```

- [ ] **Step 4: Run to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_api.py -k "batch" -q`
Expected: 7 passed (all batch tests).

- [ ] **Step 5: Commit**

```bash
git add server/api/sessions.py server/tests/test_api.py
git commit -m "feat(api): batch permanent-delete endpoint for archived sessions"
```

---

### Task 3: Frontend — `BatchResult` type + two mutation hooks

**Files:**
- Modify: `web/src/api/types.ts` (add `BatchResult`)
- Modify: `web/src/api/hooks.ts` (add `BatchResult` to type import; add two hooks after `usePermanentDeleteSession`, line 247)

**Interfaces:**
- Produces: `useBulkUnarchiveSessions()` and `useBulkPermanentDeleteSessions()`, each a `useMutation<BatchResult, ApiRequestError, string[]>`; `BatchResult = { succeeded: string[]; failed: { id: string; error: string }[] }`.
- Consumes: existing `api.post`, `QK.sessions`, `QK.archived`.

*(No new test file — these are exercised through Task 4's UI tests. This task must leave `tsc`/`eslint`/`vitest` green.)*

- [ ] **Step 1: Add the `BatchResult` type**

In `web/src/api/types.ts`, after the `ArchivedSession` interface (around line 77):

```typescript
/** Result of a best-effort batch operation over session ids. */
export interface BatchResult {
  succeeded: string[]
  failed: { id: string; error: string }[]
}
```

- [ ] **Step 2: Add the hooks**

In `web/src/api/hooks.ts`, extend the type import (lines 3-5) to include `BatchResult`:

```typescript
import type {
  Account, Session, ReauthState, ArchivedSession, AdoptionCandidate, AdoptOutcome, ModelOption, BatchResult,
} from './types'
```

Then add, immediately after `usePermanentDeleteSession` (after line 247):

```typescript
export function useBulkUnarchiveSessions() {
  const qc = useQueryClient()
  return useMutation<BatchResult, ApiRequestError, string[]>({
    mutationFn: (ids) => api.post<BatchResult>('/sessions/batch/unarchive', { ids }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.sessions })
      qc.invalidateQueries({ queryKey: QK.archived })
    },
  })
}

export function useBulkPermanentDeleteSessions() {
  const qc = useQueryClient()
  return useMutation<BatchResult, ApiRequestError, string[]>({
    mutationFn: (ids) => api.post<BatchResult>('/sessions/batch/permanent-delete', { ids }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.archived })
      // A live session can also be permanently deleted, so refresh active too.
      qc.invalidateQueries({ queryKey: QK.sessions })
    },
  })
}
```

- [ ] **Step 3: Verify types + lint + existing tests still pass**

Run (from `web/`):
```bash
node_modules/.bin/tsc --noEmit && node_modules/.bin/eslint src/api/hooks.ts src/api/types.ts && node_modules/.bin/vitest run src/components/__tests__/ArchivedTab.test.tsx
```
Expected: tsc clean, eslint clean, 16 tests pass.

- [ ] **Step 4: Commit**

```bash
git add web/src/api/types.ts web/src/api/hooks.ts
git commit -m "feat(web): bulk unarchive/permanent-delete mutation hooks"
```

---

### Task 4: Frontend — selection UI, bulk action bar, generalized confirm dialog

**Files:**
- Modify: `web/src/components/ArchivedManager.tsx` (full rewrite below)
- Modify: `web/src/components/__tests__/ArchivedTab.test.tsx` (extend mock factory + add bulk tests)

**Interfaces:**
- Consumes: `useBulkUnarchiveSessions`, `useBulkPermanentDeleteSessions`, `BatchResult` (Task 3); existing `useArchivedSessions`, `useUnarchiveSession`, `usePermanentDeleteSession`.
- Behavior produced: per-row checkbox, "Select all" header checkbox (indeterminate when partial), a bulk action bar (`role="region"`, `aria-label="Bulk actions"`) shown only when ≥1 selected with "Unarchive (N)" / "Delete (N)" / "Clear", and a result line (`role="status"`). Bulk delete reuses the generalized `PermanentDeleteDialog`.

- [ ] **Step 1: Update the test mock factory (keeps existing 16 tests green once the component calls the new hooks)**

In `web/src/components/__tests__/ArchivedTab.test.tsx`, add the two hooks to the `vi.mock('@/api/hooks', …)` factory object (after `usePermanentDeleteSession: vi.fn(),` at line 41):

```typescript
  useBulkUnarchiveSessions: vi.fn(),
  useBulkPermanentDeleteSessions: vi.fn(),
```

And add to `setupDefaultMocks()` (after the `usePermanentDeleteSession` line, line 92):

```typescript
  vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
  vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue(NOOP_MUTATION as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
```

- [ ] **Step 2: Write the failing bulk-UI tests**

Append inside the `describe('Dashboard — Archived tab', …)` block in `ArchivedTab.test.tsx`:

```typescript
  // ── Bulk operations ───────────────────────────────────────────────────────────

  function twoArchived() {
    vi.mocked(hooks.useArchivedSessions).mockReturnValue({
      data: [
        makeArchived({ id: 'arc-1', name: 'Alpha' }),
        makeArchived({ id: 'arc-2', name: 'Beta' }),
      ],
      isLoading: false,
    } as ReturnType<typeof hooks.useArchivedSessions>)
  }

  it('selecting a row reveals the bulk action bar with a count', async () => {
    const user = userEvent.setup()
    twoArchived()
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))

    expect(screen.queryByRole('region', { name: /bulk actions/i })).not.toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: /select alpha/i }))

    const bar = screen.getByRole('region', { name: /bulk actions/i })
    expect(bar).toHaveTextContent(/1 selected/i)
  })

  it('Select all selects every row', async () => {
    const user = userEvent.setup()
    twoArchived()
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))

    await user.click(screen.getByRole('checkbox', { name: /select all/i }))
    expect(screen.getByRole('region', { name: /bulk actions/i })).toHaveTextContent(/2 selected/i)
  })

  it('bulk Unarchive calls useBulkUnarchiveSessions.mutate with selected ids', async () => {
    const user = userEvent.setup()
    const bulkMutate = vi.fn()
    vi.mocked(hooks.useBulkUnarchiveSessions).mockReturnValue({
      mutate: bulkMutate, isPending: false,
    } as unknown as ReturnType<typeof hooks.useBulkUnarchiveSessions>)
    twoArchived()
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))

    await user.click(screen.getByRole('checkbox', { name: /select alpha/i }))
    await user.click(screen.getByRole('checkbox', { name: /select beta/i }))
    await user.click(screen.getByRole('button', { name: /unarchive \(2\)/i }))

    expect(bulkMutate).toHaveBeenCalledWith(
      expect.arrayContaining(['arc-1', 'arc-2']),
      expect.anything(),
    )
  })

  it('bulk Delete opens the confirm dialog and calls bulk delete on confirm', async () => {
    const user = userEvent.setup()
    const bulkDeleteMutate = vi.fn()
    vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue({
      mutate: bulkDeleteMutate, isPending: false,
    } as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
    twoArchived()
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))

    await user.click(screen.getByRole('checkbox', { name: /select alpha/i }))
    await user.click(screen.getByRole('button', { name: /^delete \(1\)/i }))

    expect(screen.getByRole('dialog')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /delete permanently/i }))
    expect(bulkDeleteMutate).toHaveBeenCalledWith(['arc-1'], expect.anything())
  })

  it('Clear deselects all and hides the action bar', async () => {
    const user = userEvent.setup()
    twoArchived()
    render(<Dashboard />, { wrapper })
    await user.click(screen.getByRole('tab', { name: /archived/i }))

    await user.click(screen.getByRole('checkbox', { name: /select alpha/i }))
    await user.click(screen.getByRole('button', { name: /clear/i }))
    expect(screen.queryByRole('region', { name: /bulk actions/i })).not.toBeInTheDocument()
  })
```

- [ ] **Step 3: Run to verify the new tests fail**

Run (from `web/`): `node_modules/.bin/vitest run src/components/__tests__/ArchivedTab.test.tsx`
Expected: the 5 new tests FAIL (no checkboxes / action bar yet); the original 16 still pass.

- [ ] **Step 4: Rewrite `ArchivedManager.tsx`**

Replace the entire contents of `web/src/components/ArchivedManager.tsx` with:

```tsx
/**
 * ArchivedManager — self-contained archived-sessions panel: the archived list
 * (select / review / unarchive / permanent-delete), a bulk action bar for
 * multi-select operations, and the permanent-delete dialog (single or bulk).
 * Owns its own data hooks; `onReviewSession` is called when the user opens a
 * read-only transcript review.
 */

import * as React from 'react'
import { useState, useRef, useEffect } from 'react'
import { Trash2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import {
  useArchivedSessions,
  useUnarchiveSession,
  usePermanentDeleteSession,
  useBulkUnarchiveSessions,
  useBulkPermanentDeleteSessions,
} from '@/api/hooks'
import type { ArchivedSession, BatchResult } from '@/api/types'

const CHECKBOX_CLS = cn(
  'h-4 w-4 rounded border-gray-300 dark:border-gray-600',
  'text-blue-600 focus-visible:ring-2 focus-visible:ring-blue-500',
)

function displayNameOf(s: ArchivedSession): string {
  return s.name || s.id.slice(0, 8)
}

function summarize(verb: string, res: BatchResult): string {
  const ok = res.succeeded.length
  const bad = res.failed.length
  return bad > 0 ? `${verb} ${ok} · ${bad} failed` : `${verb} ${ok}`
}

// ── Permanent Delete Dialog (single or bulk) ───────────────────────────────────

interface PermanentDeleteDialogProps {
  names: string[]            // 1 = single delete, >1 = bulk delete
  onClose: () => void
  onConfirm: () => void
  isPending: boolean
}

function PermanentDeleteDialog({ names, onClose, onConfirm, isPending }: PermanentDeleteDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const count = names.length

  React.useEffect(() => {
    cancelRef.current?.focus()
  }, [])

  React.useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') { onClose(); return }
      if (e.key === 'Tab') {
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button, input, select, textarea, [tabindex]:not([tabindex="-1"])',
        )
        if (!focusable || focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (e.shiftKey) {
          if (document.activeElement === first) { e.preventDefault(); last.focus() }
        } else {
          if (document.activeElement === last) { e.preventDefault(); first.focus() }
        }
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-4 bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-labelledby="perm-delete-dialog-title"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        ref={dialogRef}
        className={cn(
          'w-full max-w-sm rounded-xl',
          'bg-white dark:bg-gray-800',
          'border border-gray-200 dark:border-gray-600',
          'shadow-xl p-6 space-y-4',
        )}
      >
        <h2
          id="perm-delete-dialog-title"
          className="text-lg font-semibold text-gray-900 dark:text-gray-100"
        >
          {count === 1 ? 'Permanently delete session' : `Permanently delete ${count} sessions`}
        </h2>

        <p className="text-sm text-gray-700 dark:text-gray-300">
          {count === 1 ? (
            <>
              Permanently delete <span className="font-medium">&lsquo;{names[0]}&rsquo;</span>? This removes the
              session folder and all its data. This can&rsquo;t be undone.
            </>
          ) : (
            <>
              Permanently delete <span className="font-medium">{count} sessions</span>? This removes their
              session folders and all their data. This can&rsquo;t be undone.
            </>
          )}
        </p>

        <div className="flex gap-3 justify-end pt-2">
          <Button
            ref={cancelRef}
            type="button"
            variant="outline"
            onClick={onClose}
            disabled={isPending}
          >
            Cancel
          </Button>
          <Button
            type="button"
            onClick={onConfirm}
            disabled={isPending}
            className={cn(
              'min-h-[44px]',
              'bg-red-600 hover:bg-red-700 text-white',
              'dark:bg-red-700 dark:hover:bg-red-600',
              'focus-visible:ring-red-500',
            )}
          >
            {isPending ? 'Deleting…' : 'Delete permanently'}
          </Button>
        </div>
      </div>
    </div>
  )
}

// ── Bulk Action Bar ─────────────────────────────────────────────────────────────

interface BulkActionBarProps {
  count: number
  onUnarchive: () => void
  onDelete: () => void
  onClear: () => void
  unarchivePending: boolean
}

function BulkActionBar({ count, onUnarchive, onDelete, onClear, unarchivePending }: BulkActionBarProps) {
  return (
    <div
      role="region"
      aria-label="Bulk actions"
      className={cn(
        'flex flex-wrap items-center gap-2 rounded-lg px-3 py-2',
        'bg-gray-50 dark:bg-gray-800/60 border border-gray-200 dark:border-gray-700',
      )}
    >
      <span className="text-sm font-medium text-gray-700 dark:text-gray-300">{count} selected</span>
      <div className="flex items-center gap-2 ml-auto">
        <Button
          type="button"
          onClick={onUnarchive}
          disabled={unarchivePending}
          className={cn(
            'min-h-[44px] bg-blue-600 hover:bg-blue-700 text-white',
            'dark:bg-blue-700 dark:hover:bg-blue-600',
          )}
        >
          {unarchivePending ? 'Unarchiving…' : `Unarchive (${count})`}
        </Button>
        <Button
          type="button"
          onClick={onDelete}
          className={cn(
            'min-h-[44px] bg-red-600 hover:bg-red-700 text-white',
            'dark:bg-red-700 dark:hover:bg-red-600 focus-visible:ring-red-500',
          )}
        >
          Delete ({count})
        </Button>
        <Button type="button" variant="outline" onClick={onClear}>Clear</Button>
      </div>
    </div>
  )
}

// ── Archived List ──────────────────────────────────────────────────────────────

interface ArchivedListProps {
  sessions: ArchivedSession[]
  selected: Set<string>
  onToggle: (id: string) => void
  onToggleAll: () => void
  onReview: (id: string) => void
  onUnarchive: (id: string) => void
  onPermanentDelete: (session: ArchivedSession) => void
  unarchivingId: string | null
}

function ArchivedList({
  sessions, selected, onToggle, onToggleAll,
  onReview, onUnarchive, onPermanentDelete, unarchivingId,
}: ArchivedListProps) {
  const selectAllRef = useRef<HTMLInputElement>(null)
  const allSelected = sessions.length > 0 && sessions.every((s) => selected.has(s.id))
  const someSelected = sessions.some((s) => selected.has(s.id))

  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = someSelected && !allSelected
  }, [someSelected, allSelected])

  if (sessions.length === 0) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400 py-6 text-center">
        No archived sessions.
      </p>
    )
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 px-1">
        <input
          ref={selectAllRef}
          type="checkbox"
          checked={allSelected}
          onChange={onToggleAll}
          aria-label="Select all"
          className={CHECKBOX_CLS}
        />
        <span className="text-xs text-gray-500 dark:text-gray-400">Select all</span>
      </div>

      <ul className="space-y-2" aria-label="Archived sessions">
        {sessions.map((s) => {
          const displayName = displayNameOf(s)
          const archivedDate = new Date(s.archivedAt).toLocaleDateString(undefined, {
            year: 'numeric', month: 'short', day: 'numeric',
          })
          return (
            <li
              key={s.id}
              className={cn(
                'flex flex-col gap-2 rounded-lg border border-gray-200 dark:border-gray-700',
                'bg-white dark:bg-gray-800 px-3 py-2',
              )}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-start gap-3 flex-1 min-w-0">
                  <input
                    type="checkbox"
                    checked={selected.has(s.id)}
                    onChange={() => onToggle(s.id)}
                    aria-label={`Select ${displayName}`}
                    className={cn(CHECKBOX_CLS, 'mt-0.5')}
                  />
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{displayName}</p>
                    <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
                      {s.account} · Archived {archivedDate}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <button
                    type="button"
                    onClick={() => onReview(s.id)}
                    className={cn(
                      'rounded px-2 py-1 text-xs font-medium min-h-[44px]',
                      'border border-gray-300 dark:border-gray-600',
                      'text-gray-700 dark:text-gray-300',
                      'hover:bg-gray-100 dark:hover:bg-gray-700',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
                    )}
                    aria-label={`Review ${displayName}`}
                  >
                    Review
                  </button>
                  <button
                    type="button"
                    onClick={() => onUnarchive(s.id)}
                    disabled={unarchivingId === s.id}
                    className={cn(
                      'rounded px-2 py-1 text-xs font-medium min-h-[44px]',
                      'bg-blue-600 hover:bg-blue-700 text-white',
                      'dark:bg-blue-700 dark:hover:bg-blue-600',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
                      'disabled:opacity-40',
                    )}
                    aria-label={`Unarchive ${displayName}`}
                  >
                    {unarchivingId === s.id ? 'Restoring…' : 'Unarchive'}
                  </button>
                  <button
                    type="button"
                    onClick={() => onPermanentDelete(s)}
                    className={cn(
                      'min-h-[44px] min-w-[44px] flex items-center justify-center rounded',
                      'text-red-500 dark:text-red-400',
                      'hover:bg-red-50 dark:hover:bg-red-900/30',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500',
                    )}
                    aria-label={`Delete ${displayName} permanently`}
                  >
                    <Trash2 size={14} aria-hidden="true" />
                  </button>
                </div>
              </div>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

// ── ArchivedManager ──────────────────────────────────────────────────────────

export function ArchivedManager({ onReviewSession }: { onReviewSession: (id: string) => void }) {
  const { data: archivedSessions = [] } = useArchivedSessions()
  const unarchiveSession = useUnarchiveSession()
  const permanentDeleteSession = usePermanentDeleteSession()
  const bulkUnarchive = useBulkUnarchiveSessions()
  const bulkDelete = useBulkPermanentDeleteSessions()

  const [permDeleteTarget, setPermDeleteTarget] = useState<ArchivedSession | null>(null)
  const [unarchivingId, setUnarchivingId] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false)
  const [summary, setSummary] = useState<string | null>(null)

  // Drop selected ids that no longer exist after a refetch.
  useEffect(() => {
    setSelected((prev) => {
      if (prev.size === 0) return prev
      const present = new Set(archivedSessions.map((s) => s.id))
      const next = new Set<string>()
      let changed = false
      for (const id of prev) {
        if (present.has(id)) next.add(id)
        else changed = true
      }
      return changed ? next : prev
    })
  }, [archivedSessions])

  function toggleOne(id: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function toggleAll() {
    setSelected((prev) => {
      const allIds = archivedSessions.map((s) => s.id)
      const everySelected = allIds.length > 0 && allIds.every((id) => prev.has(id))
      return everySelected ? new Set() : new Set(allIds)
    })
  }

  function clearSelection() {
    setSelected(new Set())
  }

  function handleUnarchive(id: string) {
    setUnarchivingId(id)
    unarchiveSession.mutate(id, {
      onSettled: () => setUnarchivingId(null),
    })
  }

  function handlePermanentDelete(session: ArchivedSession) {
    setPermDeleteTarget(session)
  }

  function confirmPermanentDelete() {
    if (!permDeleteTarget) return
    permanentDeleteSession.mutate(permDeleteTarget.id, {
      onSuccess: () => setPermDeleteTarget(null),
      onError: () => setPermDeleteTarget(null),
    })
  }

  function handleBulkUnarchive() {
    const ids = [...selected]
    if (ids.length === 0) return
    bulkUnarchive.mutate(ids, {
      onSuccess: (res) => { setSummary(summarize('Unarchived', res)); setSelected(new Set()) },
    })
  }

  function confirmBulkDelete() {
    const ids = [...selected]
    if (ids.length === 0) { setBulkDeleteOpen(false); return }
    bulkDelete.mutate(ids, {
      onSuccess: (res) => { setSummary(summarize('Deleted', res)); setSelected(new Set()); setBulkDeleteOpen(false) },
      onError: () => setBulkDeleteOpen(false),
    })
  }

  const selectedNames = archivedSessions.filter((s) => selected.has(s.id)).map(displayNameOf)

  return (
    <div className="space-y-3">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
        Archived Sessions
      </h2>

      {summary && (
        <p role="status" className="text-xs text-gray-500 dark:text-gray-400">{summary}</p>
      )}

      {selected.size > 0 && (
        <BulkActionBar
          count={selected.size}
          onUnarchive={handleBulkUnarchive}
          onDelete={() => setBulkDeleteOpen(true)}
          onClear={clearSelection}
          unarchivePending={bulkUnarchive.isPending}
        />
      )}

      <ArchivedList
        sessions={archivedSessions}
        selected={selected}
        onToggle={toggleOne}
        onToggleAll={toggleAll}
        onReview={onReviewSession}
        onUnarchive={handleUnarchive}
        onPermanentDelete={handlePermanentDelete}
        unarchivingId={unarchivingId}
      />

      {permDeleteTarget && (
        <PermanentDeleteDialog
          names={[displayNameOf(permDeleteTarget)]}
          onClose={() => setPermDeleteTarget(null)}
          onConfirm={confirmPermanentDelete}
          isPending={permanentDeleteSession.isPending}
        />
      )}

      {bulkDeleteOpen && (
        <PermanentDeleteDialog
          names={selectedNames}
          onClose={() => setBulkDeleteOpen(false)}
          onConfirm={confirmBulkDelete}
          isPending={bulkDelete.isPending}
        />
      )}
    </div>
  )
}
```

- [ ] **Step 5: Run the Archived tests (new + existing)**

Run (from `web/`): `node_modules/.bin/vitest run src/components/__tests__/ArchivedTab.test.tsx`
Expected: all pass (16 original + 5 new = 21).

- [ ] **Step 6: Type-check + lint**

Run (from `web/`): `node_modules/.bin/tsc --noEmit && node_modules/.bin/eslint src/components/ArchivedManager.tsx src/components/__tests__/ArchivedTab.test.tsx`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add web/src/components/ArchivedManager.tsx web/src/components/__tests__/ArchivedTab.test.tsx
git commit -m "feat(web): bulk select + unarchive/delete on archived sessions screen"
```

---

### Task 5: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Backend — run the full sessions/archive/api suites**

Run:
```bash
env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_ENABLE_XVFB CCSWITCH_DEV=1 \
  /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest \
  server/tests/test_api.py server/tests/test_archive.py -q
```
Expected: all pass (existing + 7 new batch tests).

- [ ] **Step 2: Web — full type-check, lint, and test run**

Run (from `web/`):
```bash
node_modules/.bin/tsc --noEmit && node_modules/.bin/eslint . && node_modules/.bin/vitest run
```
Expected: tsc clean, eslint clean, all vitest suites pass.

- [ ] **Step 3: If all green, the branch is ready for PR (handled by the completion workflow).**

## Self-Review Notes

- **Spec coverage:** batch unarchive (Task 1), batch delete (Task 2), best-effort `{succeeded, failed}` + 200 (Tasks 1–2), 422 validation + 401 auth (Task 1), per-op audit (Tasks 1–2), hooks + cache invalidation (Task 3), selection/select-all/action-bar/confirm-dialog/result-summary/selection-pruning (Task 4), full verification (Task 5). All spec sections map to a task.
- **Type consistency:** `BatchResult` shape (`{succeeded: string[]; failed: {id; error}[]}`) is identical in `types.ts`, the hooks, and the backend response. Hook names `useBulkUnarchiveSessions` / `useBulkPermanentDeleteSessions` are used consistently across hooks.ts, the mock factory, and ArchivedManager. Endpoint paths `/sessions/batch/unarchive` and `/sessions/batch/permanent-delete` match between backend routes, hooks, and tests.
- **No placeholders:** every code step contains complete, copy-pasteable content.
