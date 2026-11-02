import { useState, type KeyboardEvent } from 'react'
import { cn } from '@/lib/utils'
import type { LineComment } from './reviewComments'

/**
 * The review-comment editor and the saved-comment card (D-5). Presentational:
 * both report through callbacks; the caller owns the draft. They ship in the
 * Changes tab's chunk (and, lazily, the plan card's), never in the entry.
 *
 * Native controls, not `@/ui/Button`/`Textarea`/`Input`'s `controlClass`:
 * importing any of those from a lazy chunk regroups shared chunks onto the
 * critical path (measured +0.2–1.3 kB initialJs, against a ceiling with no headroom).
 */
const ACTION = cn(
  'inline-flex min-h-target items-center justify-center rounded-md px-3 text-xs font-medium',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
  'disabled:pointer-events-none disabled:opacity-50',
)
const PRIMARY = 'bg-accent text-accent-ink hover:bg-accent/90'
const QUIET = 'text-ink-muted hover:bg-surface-raised hover:text-ink'
export default function CommentEditor({ initial = '', onSave, onCancel }: {
  initial?: string
  onSave: (body: string) => void
  onCancel: () => void
}) {
  const [body, setBody] = useState(initial)
  const trimmed = body.trim()
  const save = () => { if (trimmed) onSave(trimmed) }
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      save()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      onCancel()
    }
  }
  return (
    <div className="flex flex-col gap-2 font-sans">
      <textarea
        aria-label="Comment"
        placeholder="Leave a comment for Claude…"
        value={body}
        onChange={(e) => setBody(e.target.value)}
        onKeyDown={onKeyDown}
        autoFocus
        rows={3}
        className={cn(
          'w-full resize-y rounded-md border border-edge bg-surface px-3 py-2 text-sm leading-relaxed text-ink',
          'focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent',
        )}
      />
      <div className="flex justify-end gap-2">
        <button type="button" className={cn(ACTION, QUIET)} onClick={onCancel}>Cancel</button>
        <button type="button" className={cn(ACTION, PRIMARY)} disabled={!trimmed} onClick={save}>Save</button>
      </div>
    </div>
  )
}

/** `line 3`, `lines 2–4`, plus ` (removed)` for the old side — the card's accessible name. */
function rangeLabel({ anchor: { side, start, end } }: LineComment): string {
  return `${end > start ? `lines ${start}–${end}` : `line ${start}`}${side === 'old' ? ' (removed)' : ''}`
}

/**
 * A saved comment: its body with Edit / Delete. `stale` marks one whose lines no
 * longer match the diff — it then shows the quote it was made on (still sent).
 * `showPath` names the file, for a comment whose file left the diff entirely.
 */
export function CommentCard({ comment, stale = false, showPath = false, onEdit, onDelete }: {
  comment: LineComment
  stale?: boolean
  showPath?: boolean
  onEdit: (body: string) => void
  onDelete: () => void
}) {
  const [editing, setEditing] = useState(false)
  const label = rangeLabel(comment)
  return (
    <article
      aria-label={`Comment on ${label}`}
      className="rounded-md border border-edge bg-surface p-2 font-sans text-sm text-ink"
    >
      {(stale || showPath) && (
        <p className="mb-1 text-xs text-ink-muted">
          {stale && <span className="mr-1 rounded-sm bg-warning-wash px-1 text-ink">Outdated</span>}
          {showPath && <span className="font-mono">{comment.anchor.path}</span>}{showPath ? ' ' : ''}{label}
        </p>
      )}
      {stale && (
        <pre className="mb-1 whitespace-pre-wrap break-all border-l-2 border-edge pl-2 font-mono text-xs text-ink-muted">
          {comment.quote.join('\n')}
        </pre>
      )}
      {editing ? (
        <CommentEditor
          initial={comment.body}
          onSave={(body) => { onEdit(body); setEditing(false) }}
          onCancel={() => setEditing(false)}
        />
      ) : (
        <div className="flex items-start gap-2">
          <p className="min-w-0 flex-1 whitespace-pre-wrap break-words">{comment.body}</p>
          <button type="button" className={cn(ACTION, QUIET)} onClick={() => setEditing(true)}>Edit</button>
          <button type="button" className={cn(ACTION, QUIET)} onClick={onDelete}>Delete</button>
        </div>
      )}
    </article>
  )
}
