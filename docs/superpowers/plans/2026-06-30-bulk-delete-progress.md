# Bulk Archived-Session Delete: Progress + Cancel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the archived-sessions bulk-delete a correct count and live, moving progress (with cancel) instead of a static "Permanently delete 0 sessions / Deleting…".

**Architecture:** Frontend-only. Replace the single opaque `POST /sessions/batch/permanent-delete` call with a client-side loop that sends the selected IDs in chunks of 10, reporting `{done,total}` progress and supporting mid-run cancel. The dialog renders a frozen snapshot of the count (fixing the "0 sessions" bug) plus a progress bar.

**Tech Stack:** React 18 + TypeScript, @tanstack/react-query, Tailwind, vitest + @testing-library/react.

## Global Constraints

- Frontend only — no backend, API contract, or DB changes. The existing endpoint `POST /sessions/batch/permanent-delete` (returns `{succeeded: string[], failed: {id,error}[]}`, caps at 200 ids) is reused per chunk.
- Chunk size is **10**.
- Follow the existing progress-bar idiom from `web/src/components/session/CondensingProgress.tsx`: a `role="progressbar"` wrapper with `aria-valuemin/max/now` and an inner `<div style={{ width: `${pct}%` }}>`.
- Do **not** add a third delete-dialog variant. Modify the bulk-capable inline `PermanentDeleteDialog` inside `web/src/components/ArchivedManager.tsx`. The shared single-session `web/src/components/PermanentDeleteDialog.tsx` is out of scope; consolidation is tracked separately.
- All commands run from `web/`. The worktree has no built env — use the main checkout's `node_modules` (symlinked) per CLAUDE.md if needed.

---

### Task 1: Chunked delete hook with progress + cancel

Rewrite `useBulkPermanentDeleteSessions` to loop over chunks, expose `progress` and `cancel`, and remove succeeded IDs from the cache incrementally (replacing the up-front "remove all + rollback" optimistic update that caused the count to read 0).

**Files:**
- Modify: `web/src/api/hooks.ts:455-481` (the `useBulkPermanentDeleteSessions` function) and the React import on line 2.
- Test: `web/src/api/hooks.bulk.test.tsx` (rewrite the `useBulkPermanentDeleteSessions` describe block; leave the `useBulkUnarchiveSessions` block untouched).

**Interfaces:**
- Produces:
  - `export interface BulkDeleteProgress { done: number; total: number }`
  - `useBulkPermanentDeleteSessions()` returns the react-query mutation object augmented with `progress: BulkDeleteProgress | null` and `cancel: () => void`. The mutation variable type stays `string[]`; `mutate(ids, { onSuccess, onError })` still works and `onSuccess` receives `{ succeeded: string[]; failed: {id:string;error:string}[]; aborted: boolean }` (assignable to `BatchResult` for the existing `summarize` helper).
- Consumes: `api.post`, `QK`, `ArchivedSession`, `BatchResult` (already imported in `hooks.ts`).

- [ ] **Step 1: Rewrite the delete describe block in `hooks.bulk.test.tsx`**

Replace the entire `describe('useBulkPermanentDeleteSessions — optimistic', …)` block (lines 49-75) with the chunked-contract version below. Keep the file's existing imports, `vi.mock` calls, and the `mk` helper.

```tsx
describe('useBulkPermanentDeleteSessions — chunked', () => {
  let qc: QueryClient
  beforeEach(() => { vi.clearAllMocks(); qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

  it('splits ids into chunks of 10', async () => {
    const ids = Array.from({ length: 25 }, (_, i) => `s${i}`)
    qc.setQueryData(QK.archived, ids.map(mk))
    vi.mocked(api.post).mockImplementation(async (_url: string, body: unknown) => {
      const chunk = (body as { ids: string[] }).ids
      return { succeeded: chunk, failed: [] } as BatchResult
    })
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    result.current.mutate(ids)
    await waitFor(() => expect(result.current.isPending).toBe(false))
    expect(api.post).toHaveBeenCalledTimes(3)
    const sizes = vi.mocked(api.post).mock.calls.map((c) => (c[1] as { ids: string[] }).ids.length)
    expect(sizes).toEqual([10, 10, 5])
  })

  it('removes succeeded ids from the archived cache incrementally (failed ones stay)', async () => {
    qc.setQueryData(QK.archived, ['a', 'b', 'c'].map(mk))
    vi.mocked(api.post).mockResolvedValue({ succeeded: ['a', 'b'], failed: [{ id: 'c', error: 'busy' }] } as BatchResult)
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    result.current.mutate(['a', 'b', 'c'])
    await waitFor(() => expect(result.current.isPending).toBe(false))
    expect((qc.getQueryData<ArchivedSession[]>(QK.archived) ?? []).map((s) => s.id)).toEqual(['c'])
  })

  it('toasts a merged partial-failure summary', async () => {
    qc.setQueryData(QK.archived, ['a', 'b', 'c'].map(mk))
    vi.mocked(api.post).mockResolvedValue({ succeeded: ['a', 'b'], failed: [{ id: 'c', error: 'busy' }] } as BatchResult)
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    result.current.mutate(['a', 'b', 'c'])
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Deleted 2 · 1 failed'))
  })

  it('counts a thrown chunk as failed and continues with later chunks', async () => {
    const ids = Array.from({ length: 15 }, (_, i) => `s${i}`)
    qc.setQueryData(QK.archived, ids.map(mk))
    vi.mocked(api.post)
      .mockRejectedValueOnce(new Error('500'))
      .mockResolvedValueOnce({ succeeded: ids.slice(10), failed: [] } as BatchResult)
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    result.current.mutate(ids)
    await waitFor(() => expect(result.current.isPending).toBe(false))
    expect(api.post).toHaveBeenCalledTimes(2)
    expect(toast.error).toHaveBeenCalledWith('Deleted 5 · 10 failed')
  })

  it('reports progress and clears it when done', async () => {
    qc.setQueryData(QK.archived, ['a', 'b'].map(mk))
    vi.mocked(api.post).mockResolvedValue({ succeeded: ['a', 'b'], failed: [] } as BatchResult)
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    expect(result.current.progress).toBeNull()
    result.current.mutate(['a', 'b'])
    await waitFor(() => expect(result.current.isPending).toBe(false))
    expect(result.current.progress).toBeNull()
  })

  it('cancel stops after the current chunk', async () => {
    const ids = Array.from({ length: 15 }, (_, i) => `s${i}`)
    qc.setQueryData(QK.archived, ids.map(mk))
    let resolveFirst!: (v: BatchResult) => void
    const first = new Promise<BatchResult>((r) => { resolveFirst = r })
    vi.mocked(api.post).mockReturnValueOnce(first).mockResolvedValue({ succeeded: [], failed: [] } as BatchResult)
    const { result } = renderHook(() => useBulkPermanentDeleteSessions(), { wrapper })
    result.current.mutate(ids)
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1))
    result.current.cancel()
    resolveFirst({ succeeded: ids.slice(0, 10), failed: [] })
    await waitFor(() => expect(result.current.isPending).toBe(false))
    expect(api.post).toHaveBeenCalledTimes(1)
  })
})
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `npx vitest run src/api/hooks.bulk.test.tsx`
Expected: FAIL — the delete tests fail (current hook does one POST, has no `progress`/`cancel`). The unarchive block still passes.

- [ ] **Step 3: Rewrite the hook**

In `web/src/api/hooks.ts`, change the React import on line 2 to add `useState`, `useRef`, `useCallback`:

```ts
import { useEffect, useMemo, useState, useRef, useCallback, useSyncExternalStore } from 'react'
```

Replace the whole `useBulkPermanentDeleteSessions` function (lines 455-481) with:

```ts
export interface BulkDeleteProgress {
  done: number
  total: number
}

const DELETE_CHUNK_SIZE = 10

/**
 * Permanently delete many archived sessions, chunked for live progress + cancel.
 *
 * The backend processes a batch sequentially and only replies when the whole
 * batch is done, so a single 95-id request is a multi-minute black box. We slice
 * the ids into chunks and fire one request per chunk, advancing `progress` after
 * each. Succeeded ids are pruned from the cache incrementally so the list shrinks
 * in step with the bar. Best-effort: a chunk whose request throws has its ids
 * counted as failed and the loop continues. `cancel()` stops the loop at the next
 * chunk boundary; already-deleted sessions stay deleted.
 */
export function useBulkPermanentDeleteSessions() {
  const qc = useQueryClient()
  const [progress, setProgress] = useState<BulkDeleteProgress | null>(null)
  const cancelRef = useRef(false)

  const mutation = useMutation<BatchResult & { aborted: boolean }, ApiRequestError, string[]>({
    mutationFn: async (ids) => {
      cancelRef.current = false
      const succeeded: string[] = []
      const failed: { id: string; error: string }[] = []
      setProgress({ done: 0, total: ids.length })
      for (let i = 0; i < ids.length; i += DELETE_CHUNK_SIZE) {
        if (cancelRef.current) break
        const chunk = ids.slice(i, i + DELETE_CHUNK_SIZE)
        try {
          const res = await api.post<BatchResult>('/sessions/batch/permanent-delete', { ids: chunk })
          succeeded.push(...res.succeeded)
          failed.push(...res.failed)
          const okSet = new Set(res.succeeded)
          qc.setQueryData<ArchivedSession[]>(QK.archived, (cur) => (cur ?? []).filter((x) => !okSet.has(x.id)))
        } catch {
          for (const id of chunk) failed.push({ id, error: 'request failed' })
        }
        setProgress({ done: Math.min(i + DELETE_CHUNK_SIZE, ids.length), total: ids.length })
      }
      return { succeeded, failed, aborted: cancelRef.current }
    },
    onSuccess: (result) => {
      const n = result.succeeded.length
      const f = result.failed.length
      const stopped = result.aborted ? ' · stopped' : ''
      if (f > 0) toast.error(`Deleted ${n} · ${f} failed${stopped}`)
      else toast.success(`Deleted ${n}${stopped}`)
    },
    onSettled: () => {
      setProgress(null)
      cancelRef.current = false
      qc.invalidateQueries({ queryKey: QK.archived })
      qc.invalidateQueries({ queryKey: QK.sessions })
    },
  })

  const cancel = useCallback(() => { cancelRef.current = true }, [])

  return { ...mutation, progress, cancel }
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `npx vitest run src/api/hooks.bulk.test.tsx`
Expected: PASS — all delete tests + the untouched unarchive tests.

- [ ] **Step 5: Typecheck**

Run: `npx tsc --noEmit`
Expected: no errors. (If `tsc` reports an unrelated pre-existing error elsewhere, note it and continue; this task must not add new ones.)

- [ ] **Step 6: Commit**

```bash
git add web/src/api/hooks.ts web/src/api/hooks.bulk.test.tsx
git commit -m "feat(archived): chunked bulk delete with progress + cancel in hook

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Dialog progress UI + frozen-count snapshot

Wire the hook's `progress`/`cancel` into the inline bulk dialog and snapshot the count at confirm time so it never reads 0.

**Files:**
- Modify: `web/src/components/ArchivedManager.tsx` — the inline `PermanentDeleteDialog` (props + body, lines 51-153), the `ArchivedManager` state/handlers (lines ~221-319), the `BulkActionBar` `onDelete` wiring (line 430), and the bulk dialog render (lines 515-522). Add `BulkDeleteProgress` to the `@/api/hooks` import.
- Test: `web/src/components/__tests__/ArchivedManager.test.tsx` (add a new describe block; update the bulk-delete mock shape in `setupMocks`).

**Interfaces:**
- Consumes from Task 1: `BulkDeleteProgress`, and `useBulkPermanentDeleteSessions()` returning `{ mutate, isPending, progress, cancel, … }`.
- Produces: no new exports (component-internal changes).

- [ ] **Step 1: Add the failing component tests**

In `ArchivedManager.test.tsx`, first update the bulk-delete mock in `setupMocks` (line 75) so the component can read `progress`/`cancel`:

```tsx
  vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue({ ...NOOP_MUTATION, progress: null, cancel: vi.fn() } as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
```

Then append this describe block at the end of the file:

```tsx
describe('ArchivedManager — bulk delete progress', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    searchState = { query: '', setQuery: vi.fn(), matchedIds: null, isFiltered: false }
  })

  it('keeps the frozen count after the archived list empties mid-delete', async () => {
    const user = userEvent.setup()
    setupMocks(fourArchived())
    const { rerender } = render(<ArchivedManager onReviewSession={vi.fn()} />, { wrapper })

    await user.click(screen.getByRole('checkbox', { name: /select all/i }))
    await user.click(screen.getByRole('button', { name: /delete \(4\)/i }))
    expect(screen.getByText('Permanently delete 4 sessions')).toBeInTheDocument()

    // Simulate the optimistic cache emptying out from under the open dialog.
    vi.mocked(hooks.useArchivedSessions).mockReturnValue({ data: [], isLoading: false } as ReturnType<typeof hooks.useArchivedSessions>)
    rerender(<ArchivedManager onReviewSession={vi.fn()} />)

    expect(screen.getByText('Permanently delete 4 sessions')).toBeInTheDocument()
  })

  it('renders a progress bar with "X of N" while deleting', async () => {
    const user = userEvent.setup()
    setupMocks(fourArchived())
    vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue({
      mutate: vi.fn(), isPending: true, progress: { done: 2, total: 4 }, cancel: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
    render(<ArchivedManager onReviewSession={vi.fn()} />, { wrapper })

    await user.click(screen.getByRole('checkbox', { name: /select all/i }))
    await user.click(screen.getByRole('button', { name: /delete \(4\)/i }))

    expect(screen.getByRole('progressbar')).toBeInTheDocument()
    expect(screen.getByText(/2 of 4/)).toBeInTheDocument()
  })

  it('Cancel during a delete calls the hook cancel(), not close', async () => {
    const user = userEvent.setup()
    const cancel = vi.fn()
    setupMocks(fourArchived())
    vi.mocked(hooks.useBulkPermanentDeleteSessions).mockReturnValue({
      mutate: vi.fn(), isPending: true, progress: { done: 1, total: 4 }, cancel,
    } as unknown as ReturnType<typeof hooks.useBulkPermanentDeleteSessions>)
    render(<ArchivedManager onReviewSession={vi.fn()} />, { wrapper })

    await user.click(screen.getByRole('checkbox', { name: /select all/i }))
    await user.click(screen.getByRole('button', { name: /delete \(4\)/i }))
    await user.click(screen.getByRole('button', { name: /^cancel$/i }))

    expect(cancel).toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `npx vitest run src/components/__tests__/ArchivedManager.test.tsx`
Expected: FAIL — frozen-count test sees "0 sessions" after rerender; progress-bar and Cancel tests fail (no progressbar / Cancel disabled).

- [ ] **Step 3: Extend the dialog import + props**

In `ArchivedManager.tsx`, add `BulkDeleteProgress` to the hooks import (find the line importing `useBulkPermanentDeleteSessions` from `@/api/hooks` and add the type). Then replace the `PermanentDeleteDialogProps` interface (lines 51-56) with:

```tsx
interface PermanentDeleteDialogProps {
  names: string[]            // 1 = single delete, >1 = bulk delete
  onClose: () => void
  onConfirm: () => void
  isPending: boolean
  progress?: BulkDeleteProgress | null   // bulk only: drives the progress bar
  onCancel?: () => void                  // bulk only: abort mid-run
}
```

- [ ] **Step 4: Update the dialog body**

Replace the function signature line 58 and the descriptive `<p>` + button block so a progress bar shows while deleting and Cancel aborts.

Signature (line 58):

```tsx
function PermanentDeleteDialog({ names, onClose, onConfirm, isPending, progress, onCancel }: PermanentDeleteDialogProps) {
```

Guard the backdrop and Esc against closing while pending. Change the backdrop `onClick` (line 94):

```tsx
      onClick={(e) => { if (e.target === e.currentTarget && !isPending) onClose() }}
```

And the Escape branch inside the keydown handler (line 69):

```tsx
      if (e.key === 'Escape') { if (!isPending) onClose(); return }
```

Replace the descriptive `<p>…</p>` block (lines 112-124) with a conditional that swaps in the progress bar while deleting:

```tsx
        {isPending && progress ? (
          <div className="space-y-2">
            <div className="flex items-center justify-between text-sm text-gray-700 dark:text-gray-300">
              <span>Deleting…</span>
              <span className="tabular-nums">{progress.done} of {progress.total}</span>
            </div>
            <div
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={progress.total}
              aria-valuenow={progress.done}
              aria-valuetext={`Deleting ${progress.done} of ${progress.total}`}
              className="h-2 w-full overflow-hidden rounded bg-gray-200 dark:bg-gray-700"
            >
              <div
                className="h-full rounded bg-red-600 transition-[width] duration-200 ease-linear"
                style={{ width: `${progress.total > 0 ? Math.round((progress.done / progress.total) * 100) : 0}%` }}
              />
            </div>
          </div>
        ) : (
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
        )}
```

Update the Cancel button (lines 127-135) so it aborts while deleting (bulk) but stays disabled while a single delete is in flight (no `onCancel`):

```tsx
          <Button
            ref={cancelRef}
            type="button"
            variant="outline"
            onClick={isPending ? onCancel : onClose}
            disabled={isPending && !onCancel}
          >
            Cancel
          </Button>
```

The confirm button (lines 136-148) is unchanged — it stays `disabled={isPending}` with the `{isPending ? 'Deleting…' : 'Delete permanently'}` label.

- [ ] **Step 5: Snapshot the selection and wire the bulk dialog in `ArchivedManager`**

Add snapshot state next to the other `useState` calls (after line 224):

```tsx
  const [bulkSnapshot, setBulkSnapshot] = useState<{ ids: string[]; names: string[] } | null>(null)
```

Add an opener that freezes the count, placed near the other handlers (e.g. after `handleBulkUnarchive`, line 308):

```tsx
  function openBulkDelete() {
    const sel = archivedSessions.filter((s) => selected.has(s.id))
    if (sel.length === 0) return
    setBulkSnapshot({ ids: sel.map((s) => s.id), names: sel.map(displayNameOf) })
    setBulkDeleteOpen(true)
  }
```

Replace `confirmBulkDelete` (lines 310-317) to delete from the snapshot and clear it:

```tsx
  function confirmBulkDelete() {
    const ids = bulkSnapshot?.ids ?? []
    if (ids.length === 0) { setBulkDeleteOpen(false); setBulkSnapshot(null); return }
    bulkDelete.mutate(ids, {
      onSuccess: (res) => {
        setSummary(summarize('Deleted', res))
        setSelected(new Set())
        setBulkDeleteOpen(false)
        setBulkSnapshot(null)
      },
      onError: () => { setBulkDeleteOpen(false); setBulkSnapshot(null) },
    })
  }
```

Change the `BulkActionBar` delete wiring (line 430) from `onDelete={() => setBulkDeleteOpen(true)}` to:

```tsx
          onDelete={openBulkDelete}
```

Replace the bulk dialog render (lines 515-522) to use the snapshot and pass progress/cancel:

```tsx
      {bulkDeleteOpen && bulkSnapshot && (
        <PermanentDeleteDialog
          names={bulkSnapshot.names}
          onClose={() => { setBulkDeleteOpen(false); setBulkSnapshot(null) }}
          onConfirm={confirmBulkDelete}
          isPending={bulkDelete.isPending}
          progress={bulkDelete.progress}
          onCancel={bulkDelete.cancel}
        />
      )}
```

The now-unused `selectedNames` const (line 319) is no longer read by the dialog. Remove it if nothing else references it (grep `selectedNames` in the file first); if another consumer exists, leave it.

- [ ] **Step 6: Run the component tests to verify they pass**

Run: `npx vitest run src/components/__tests__/ArchivedManager.test.tsx`
Expected: PASS — including the existing controls/shift-click tests and the three new ones.

- [ ] **Step 7: Typecheck + lint**

Run: `npx tsc --noEmit && npx eslint src/components/ArchivedManager.tsx src/api/hooks.ts`
Expected: no errors (no `selectedNames` unused-var warning, no missing-dep warnings).

- [ ] **Step 8: Commit**

```bash
git add web/src/components/ArchivedManager.tsx web/src/components/__tests__/ArchivedManager.test.tsx
git commit -m "feat(archived): bulk-delete dialog shows live progress, cancel, frozen count

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Full-suite verification

Confirm nothing else regressed (the hook's contract changed; `ArchivedTab` and other consumers exercise it).

**Files:** none (verification only).

- [ ] **Step 1: Run the full web suite**

Run (from `web/`): `npx vitest run && npx tsc --noEmit && npx eslint .`
Expected: all green. If any other test asserted the old single-POST/optimistic-rollback delete behavior, update it to the chunked contract (a thrown request no longer rolls the cache back; succeeded ids are pruned per chunk) and re-run.

- [ ] **Step 2: Commit any follow-up fixes**

```bash
git add -A
git commit -m "test(archived): align remaining tests with chunked bulk delete

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

(Skip this commit if Step 1 was clean with no edits.)

---

## Self-Review

- **Spec coverage:** chunking (T1 step 3), progress bar + "X of N" (T2 step 4), frozen count via snapshot (T2 step 5), cancel after current chunk (T1 hook + T2 wiring), incremental cache removal (T1), merged best-effort summary incl. thrown chunk (T1 tests), no backend change (reuses endpoint), follow CondensingProgress idiom (T2 step 4), no third dialog variant (modifies inline copy). All covered.
- **Placeholder scan:** none — every code step shows full content.
- **Type consistency:** `BulkDeleteProgress {done,total}` defined in T1, imported and used identically in T2 props and tests; `cancel: () => void` and `progress: BulkDeleteProgress | null` consistent across hook return, dialog props, and mocks; `mutate(ids, {onSuccess,onError})` result includes `succeeded/failed/aborted`, and `summarize` only reads `succeeded/failed`.
