import { useCallback, useRef, useState } from 'react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import { useSessionDiff } from '@/api/hooks'
import { useViewport } from '@/shell/useViewport'
import CommentableDiff from '../review/CommentableDiff'
import { CommentCard } from '../review/CommentEditor'
import { composeReviewPrompt, newCommentId, type CommentAnchor, type LineComment } from '../review/reviewComments'
import { useReviewDraft } from '../review/useReviewDraft'
import type { DiffViewMode } from './DiffRenderer'
import { getDiffViewPreference, setDiffViewPreference } from './diffViewPreference'
import type { FileDiff } from './types'

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
 * as ONE prompt through `onSendReview` — the runtime's `onPrompt`, the function
 * the composer's Send and Queue call, so a busy session queues it exactly as a
 * typed prompt would be (the server queues behind the live turn).
 */
export default function ChangesTab({ sessionId, onSendReview }: {
  sessionId: string
  onSendReview: (text: string, attachmentIds: string[]) => void
}) {
  const [ignoreWhitespace, setIgnoreWhitespace] = useState(false)
  const { data, isError, refetch } = useSessionDiff(sessionId, { ignoreWhitespace })
  const [view, setView] = useDiffView()
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set())
  const listRef = useRef<HTMLDivElement>(null)
  const goToChange = useChangeNavigation(listRef)
  const draft = useReviewDraft(sessionId)
  const review: ReviewHandlers = {
    add: (anchor, quote, body) => draft.add({ id: newCommentId(), anchor, quote, body }),
    edit: draft.update,
    remove: draft.remove,
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
  const paths = new Set(data.files.map((f) => f.path))
  // A comment whose file left the diff (reverted, or hidden by the whitespace
  // toggle) is still in the draft and still sent — so it stays visible.
  const orphans = draft.comments.filter((c) => !paths.has(c.anchor.path ?? ''))

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col" data-testid="changes-tab">
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
                           onEdit={(b) => draft.update(c.id, b)} onDelete={() => draft.remove(c.id)} />
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
            comments={draft.comments.filter((c) => c.anchor.path === f.path)}
            review={review}
          />
        ))}
      </div>

      {draft.comments.length > 0 && (
        <ReviewBar comments={draft.comments} onDiscard={draft.clear} onSend={() => {
          const n = draft.comments.length
          onSendReview(composeReviewPrompt(draft.comments), [])
          draft.clear()
          toast.success(`Sent ${countLabel(n)}`)
        }} />
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

/**
 * Shown while the draft has comments: the count, Discard all, Send to Claude.
 * Discard confirms in place (Discard / Keep) rather than through `ConfirmDialog`:
 * importing the dialog into this lazy chunk regroups shared chunks onto the
 * critical path (+0.8 kB initialJs against a ceiling with no headroom).
 */
function ReviewBar({ comments, onDiscard, onSend }: {
  comments: readonly LineComment[]; onDiscard: () => void; onSend: () => void
}) {
  const [confirming, setConfirming] = useState(false)
  const n = countLabel(comments.length)
  return (
    <div role="region" aria-label="Review comments"
         className="flex shrink-0 flex-wrap items-center gap-2 border-t border-edge bg-surface px-3 py-1">
      {confirming ? (
        <>
          <span role="alert" className="min-w-0 flex-1 text-sm text-ink">{`Discard ${n}? They won’t be sent.`}</span>
          <button type="button" autoFocus onClick={() => setConfirming(false)} className={cn(CONTROL, UNPRESSED)}>Keep</button>
          <button type="button" onClick={() => { setConfirming(false); onDiscard() }}
                  className={cn(CONTROL, 'bg-danger-strong px-3 text-on-strong hover:bg-danger-strong/90')}>
            Discard
          </button>
        </>
      ) : (
        <>
          <span className="min-w-0 flex-1 text-sm text-ink">{n}</span>
          <button type="button" onClick={() => setConfirming(true)} className={cn(CONTROL, UNPRESSED)}>Discard all</button>
          <button type="button" onClick={onSend} className={cn(CONTROL, 'bg-accent px-3 text-accent-ink hover:bg-accent/90')}>
            Send to Claude
          </button>
        </>
      )}
    </div>
  )
}

function FileSection({ file, view, open, onToggle, truncated, comments, review }: {
  file: FileDiff; view: DiffViewMode; open: boolean; onToggle: () => void; truncated: boolean
  comments: LineComment[]; review: ReviewHandlers
}) {
  const name = file.oldPath && file.oldPath !== file.path ? `${file.oldPath} → ${file.path}` : file.path
  return (
    <section className="min-w-0 rounded-md border border-edge">
      <button
        type="button"
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
  file: FileDiff; view: DiffViewMode; truncated: boolean; comments: LineComment[]; review: ReviewHandlers
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
