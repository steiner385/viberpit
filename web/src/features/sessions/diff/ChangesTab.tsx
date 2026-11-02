import { useCallback, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import { useSessionDiff } from '@/api/hooks'
import { useViewport } from '@/shell/useViewport'
import { Button } from '@/ui/Button'
import { TOUCH_FLOOR } from '@/ui/list/listControlStyles'
import { useConfirm } from '@/ui/useConfirm'
import type { PromptSendResult } from '@/ws/client'
import CommentableDiff from '../review/CommentableDiff'
import { CommentCard } from '../review/CommentEditor'
import { staleCommentIds } from '../review/diffAnchors'
import { afterDelete, useReturnFocus } from '../review/returnFocus'
import { composeReviewPrompt, newCommentId, type CommentAnchor, type LineComment } from '../review/reviewComments'
import { useReviewDraft } from '../review/useReviewDraft'
import type { DiffViewMode } from './DiffRenderer'
import { getDiffViewPreference, setDiffViewPreference } from './diffViewPreference'
import type { DiffHunk, FileDiff } from './types'

/** Per-file render cap; the renderer's own "show all" button lifts it. */
const MAX_LINES = 1500

const CONTROL = cn(
  'inline-flex min-h-target items-center justify-center gap-1 rounded-md px-2 text-xs font-medium',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
  'disabled:pointer-events-none disabled:opacity-50',
)
const PRESSED = 'bg-accent-wash text-accent'
const UNPRESSED = 'text-ink-muted hover:bg-surface-raised hover:text-ink'

/**
 * The session's Changes tab (D-4, spec 2026-11-01 §2): the worktree's diff
 * against its base, one collapsible section per file, each drawn by the shared
 * `DiffRenderer`.
 *
 * Loaded with `React.lazy` from `SessionBody` — only the header chip ships in
 * the entry. It fetches the FULL diff (hunks included) when it mounts, which is
 * the "refetch when the tab opens"; a turn ending invalidates it through the
 * summary hook's `busy` edge in `SessionBody`.
 *
 * Line comments (D-5) collect in a per-session draft; the review bar sends them
 * as ONE prompt through `onSendReview` — the runtime's `onPlainPrompt`: the
 * composer's own send/queue path (busy = queued behind the live turn, closed
 * socket = held for the reconnect) minus chat-arming, so a review is never
 * delivered as an armed question's answer. The draft clears only once the send
 * reports a stage; a throw keeps it. When the review bar goes (Send or Discard),
 * focus moves to the first file's header — or this panel, with no files.
 */
export default function ChangesTab({ sessionId, onSendReview }: {
  sessionId: string
  onSendReview: (text: string, attachmentIds: string[]) => PromptSendResult
}) {
  const [ignoreWhitespace, setIgnoreWhitespace] = useState(false)
  const { data, isError, refetch } = useSessionDiff(sessionId, { ignoreWhitespace })
  const [view, setView] = useDiffView()
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set())
  const listRef = useRef<HTMLDivElement>(null)
  const goToChange = useChangeNavigation(listRef)
  const draft = useReviewDraft(sessionId)
  const { add: addComment, update, remove } = draft
  const review = useMemo<ReviewHandlers>(() => ({
    add: (anchor, quote, body) => addComment({ id: newCommentId(), anchor, quote, body }),
    edit: update,
    remove,
  }), [addComment, update, remove])
  const byPath = useCommentsByPath(draft.comments)
  const { root, returnFocus } = useReturnFocus<HTMLDivElement>()
  const firstHeader = (r: HTMLDivElement) => r.querySelector<HTMLElement>('[data-file-header]') ?? r
  const clearDraft = () => {
    returnFocus(firstHeader)
    draft.clear()
  }

  const toggle = (path: string) => setCollapsed((prev) => {
    const next = new Set(prev)
    if (!next.delete(path)) next.add(path)
    return next
  })

  if (!data) {
    if (isError) {
      return (
        <div className="p-4 text-sm text-ink-muted">
          Couldn&apos;t load the changes.{' '}
          <button type="button" onClick={() => void refetch()} className={cn(CONTROL, 'text-accent hover:underline')}>
            Retry
          </button>
        </div>
      )
    }
    return <div className="p-4 text-sm text-ink-muted" aria-busy="true">Loading changes…</div>
  }

  const hasHunks = data.files.some((f) => f.hunks.length > 0)
  const hunksByPath = new Map<string, readonly DiffHunk[]>(data.files.map((f) => [f.path, f.hunks]))
  // A comment whose file left the diff (reverted, or hidden by the whitespace
  // toggle) is still in the draft and still sent — so it stays visible.
  const orphans = draft.comments.filter((c) => !hunksByPath.has(c.anchor.path ?? ''))

  const send = () => {
    const n = draft.comments.length
    let stage: PromptSendResult
    try {
      stage = onSendReview(composeReviewPrompt(draft.comments, staleCommentIds(draft.comments, hunksByPath)), [])
    } catch {
      toast.error('Couldn’t send the review — your comments are kept.')
      return
    }
    clearDraft()
    toast.success(stage === 'sent'
      ? `Sent ${countLabel(n)}`
      : `Queued ${countLabel(n)} — they send when the connection is back`)
  }

  return (
    <div ref={root} tabIndex={-1} data-review-root className="flex h-full min-h-0 min-w-0 flex-col focus:outline-none" data-testid="changes-tab">
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-edge bg-canvas px-3 py-1">
        <div role="group" aria-label="Diff layout" className="flex rounded-md border border-edge">
          {(['unified', 'split'] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              aria-pressed={view === mode}
              onClick={() => setView(mode)}
              className={cn(CONTROL, view === mode ? PRESSED : UNPRESSED)}
            >
              {mode === 'unified' ? 'Unified' : 'Split'}
            </button>
          ))}
        </div>
        <button
          type="button"
          aria-pressed={ignoreWhitespace}
          onClick={() => setIgnoreWhitespace((v) => !v)}
          className={cn(CONTROL, ignoreWhitespace ? PRESSED : UNPRESSED)}
        >
          Hide whitespace
        </button>
        <div className="flex">
          <button type="button" aria-label="Previous change" title="Previous change" disabled={!hasHunks}
                  onClick={() => goToChange(-1)} className={cn(CONTROL, UNPRESSED, 'min-w-target')}>
            <span aria-hidden="true">↑</span>
          </button>
          <button type="button" aria-label="Next change" title="Next change" disabled={!hasHunks}
                  onClick={() => goToChange(1)} className={cn(CONTROL, UNPRESSED, 'min-w-target')}>
            <span aria-hidden="true">↓</span>
          </button>
        </div>
        <span className="min-w-0 text-xs text-ink-muted">
          {data.base
            ? <span className="font-mono">{`vs ${data.base.slice(0, 7)}`}</span>
            : 'Uncommitted changes only — no default branch to compare against'}
        </span>
      </div>

      <div ref={listRef} className="min-h-0 min-w-0 flex-1 overflow-y-auto p-2 space-y-2">
        {data.truncated && (
          <p role="note" className="rounded-md border border-edge bg-warning-wash px-3 py-2 text-xs text-ink">
            Diff truncated — files past the limit are listed without their changes, and the totals are lower bounds.
          </p>
        )}
        {orphans.length > 0 && (
          <section aria-label="Comments on files no longer changed" className="space-y-1">
            {orphans.map((c) => (
              <CommentCard key={c.id} comment={c} stale showPath
                           onEdit={(b) => draft.update(c.id, b)}
                           onDelete={() => {
                             returnFocus(afterDelete(root.current, c.id, firstHeader))
                             draft.remove(c.id)
                           }} />
            ))}
          </section>
        )}
        {data.files.map((f) => (
          <FileSection
            key={f.path}
            file={f}
            view={view}
            open={!collapsed.has(f.path)}
            onToggle={() => toggle(f.path)}
            truncated={data.truncated}
            comments={byPath.get(f.path) ?? NO_COMMENTS}
            review={review}
          />
        ))}
      </div>

      {draft.comments.length > 0 && (
        <ReviewBar comments={draft.comments} onDiscard={clearDraft} onSend={send} />
      )}
    </div>
  )
}

type ReviewHandlers = {
  add: (anchor: CommentAnchor, quote: string[], body: string) => void
  edit: (id: string, body: string) => void
  remove: (id: string) => void
}

const countLabel = (n: number) => `${n} comment${n === 1 ? '' : 's'}`

const NO_COMMENTS: readonly LineComment[] = []

/**
 * The draft split per file path. A file whose comments are unchanged keeps the
 * SAME array across draft edits, so its `CommentableDiff` decorations (and the
 * memoized renderer rows behind them) are not recomputed by an edit elsewhere.
 */
function useCommentsByPath(comments: readonly LineComment[]): ReadonlyMap<string, readonly LineComment[]> {
  const [cache, setCache] = useState(() => ({ src: comments, out: splitByPath(comments, new Map()) }))
  if (cache.src === comments) return cache.out
  // The draft changed: re-split during render (no stale frame), reusing unchanged arrays.
  const out = splitByPath(comments, cache.out)
  setCache({ src: comments, out })
  return out
}

function splitByPath(
  comments: readonly LineComment[], prev: ReadonlyMap<string, readonly LineComment[]>,
): ReadonlyMap<string, readonly LineComment[]> {
  const next = new Map<string, LineComment[]>()
  for (const c of comments) {
    const key = c.anchor.path ?? ''
    next.set(key, [...(next.get(key) ?? []), c])
  }
  const out = new Map<string, readonly LineComment[]>()
  for (const [path, list] of next) {
    const old = prev.get(path)
    out.set(path, old && old.length === list.length && old.every((c, i) => c === list[i]) ? old : list)
  }
  return out
}

/** Shown while the draft has comments: the count, Discard all (confirmed), Send to Claude. */
function ReviewBar({ comments, onDiscard, onSend }: {
  comments: readonly LineComment[]; onDiscard: () => void; onSend: () => void
}) {
  const { confirm, dialog } = useConfirm()
  const discard = async () => {
    const ok = await confirm({
      title: 'Discard all comments?',
      description: `${countLabel(comments.length)} will be deleted without being sent.`,
      confirmLabel: 'Discard',
    })
    if (ok) onDiscard()
  }
  return (
    <div role="region" aria-label="Review comments"
         className="flex shrink-0 flex-wrap items-center gap-2 border-t border-edge bg-surface px-3 py-1">
      <span className="min-w-0 flex-1 text-sm text-ink">{countLabel(comments.length)}</span>
      <Button type="button" variant="ghost" size="sm" className={TOUCH_FLOOR} onClick={() => void discard()}>
        Discard all
      </Button>
      <Button type="button" size="sm" className={TOUCH_FLOOR} onClick={onSend}>Send to Claude</Button>
      {dialog}
    </div>
  )
}

function FileSection({ file, view, open, onToggle, truncated, comments, review }: {
  file: FileDiff; view: DiffViewMode; open: boolean; onToggle: () => void; truncated: boolean
  comments: readonly LineComment[]; review: ReviewHandlers
}) {
  const name = file.oldPath && file.oldPath !== file.path ? `${file.oldPath} → ${file.path}` : file.path
  return (
    <section data-file className="min-w-0 rounded-md border border-edge">
      <button
        type="button"
        data-file-header
        aria-expanded={open}
        onClick={onToggle}
        title={name}
        className={cn(
          'flex min-h-target w-full min-w-0 items-center gap-2 px-2 text-left text-xs hover:bg-surface-raised',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        )}
      >
        <span aria-hidden="true" className="w-3 shrink-0 text-ink-muted">{open ? '▾' : '▸'}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-ink">{name}</span>
        <span className="shrink-0 text-ink-muted">{file.status}</span>
        <span className="shrink-0 font-mono tabular-nums">
          <span className="text-success">+{file.additions}</span>{' '}
          <span className="text-danger">−{file.deletions}</span>
        </span>
      </button>
      {open && <FileBody file={file} view={view} truncated={truncated} comments={comments} review={review} />}
    </section>
  )
}

function FileBody({ file, view, truncated, comments, review }: {
  file: FileDiff; view: DiffViewMode; truncated: boolean; comments: readonly LineComment[]; review: ReviewHandlers
}) {
  // Binary and too-large files carry the renderer's own notice.
  if (file.hunks.length === 0 && !file.binary && !file.tooLarge) {
    return (
      <p className="border-t border-edge px-3 py-2 text-xs text-ink-muted">
        {truncated ? 'Changes not loaded — past the diff’s size limit.' : 'No line changes (an empty file or a mode change).'}
      </p>
    )
  }
  // Keyed per file: the renderer's "show all" state must not carry to another file.
  return (
    <CommentableDiff
      key={file.path}
      file={file}
      view={view}
      maxLines={MAX_LINES}
      className="border-t border-edge"
      comments={comments}
      onAddComment={review.add}
      onEditComment={review.edit}
      onDeleteComment={review.remove}
    />
  )
}

/** Unified below 1024px, split from 1024px, unless the user has chosen — then their choice, per browser. */
function useDiffView(): [DiffViewMode, (m: DiffViewMode) => void] {
  const { width } = useViewport()
  const [chosen, setChosen] = useState<DiffViewMode | null>(getDiffViewPreference)
  const set = useCallback((m: DiffViewMode) => {
    setDiffViewPreference(m)
    setChosen(m)
  }, [])
  return [chosen ?? (width === 'desktop' || width === 'wide' ? 'split' : 'unified'), set]
}

const LANDED = ['relative', 'z-10', 'ring-2', 'ring-accent']

const isChanged = (row: Element) =>
  row.matches('[data-kind="add"],[data-kind="del"]') || row.querySelector('[data-kind="add"],[data-kind="del"]') !== null

/**
 * Previous/Next change: step through the first row of each contiguous add/del
 * run in the rendered diffs (both views: a unified row carries `data-kind`, a
 * split row's cells do), scroll it into view and focus it. Collapsed files have
 * no rows, so they are skipped.
 */
function useChangeNavigation(listRef: React.RefObject<HTMLDivElement | null>) {
  const cursor = useRef(-1)
  return useCallback((dir: 1 | -1) => {
    const rows = Array.from(listRef.current?.querySelectorAll<HTMLElement>('[data-diff-row]') ?? [])
    const starts = rows.filter((r) => {
      if (!isChanged(r)) return false
      const prev = r.previousElementSibling
      return !(prev?.hasAttribute('data-diff-row') && isChanged(prev))
    })
    if (starts.length === 0) return
    const at = starts.indexOf(document.activeElement as HTMLElement)
    const from = at >= 0 ? at : Math.min(cursor.current, starts.length - 1)
    const next = dir > 0 ? Math.min(from + 1, starts.length - 1) : Math.max(from - 1, 0)
    const el = starts[next]
    cursor.current = next
    el.tabIndex = -1
    // Mark where it landed. A click-driven focus() does not match :focus-visible,
    // so the ring is set here (utilities the app already ships — no new CSS) and
    // dropped when focus moves on.
    el.classList.add(...LANDED)
    el.addEventListener('blur', () => el.classList.remove(...LANDED), { once: true })
    el.focus({ preventScroll: true })
    el.scrollIntoView({ block: 'center' })
  }, [listRef])
}

/** The plan card's lazy per-step comment view rides this chunk, beside the gutter and editor it reuses
 *  (`PermissionCard` imports it from here; a chunk of its own would regroup the shared chunks). */
export { default as PlanStepLines } from '../review/PlanStepLines'
