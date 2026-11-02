import { useState, type KeyboardEvent } from 'react'
import { Button } from '@/ui/Button'
import { Textarea } from '@/ui/Textarea'
import type { CommentAnchor, LineComment } from './reviewComments'

/**
 * The review-comment editor and the saved-comment card (D-5). Presentational:
 * both report through callbacks; the caller owns the draft. They ship in the
 * Changes tab's chunk (and, lazily, the plan card's), never in the entry.
 *
 * Buttons are `Button size="sm"` with the coarse-pointer 44px floor
 * (docs/ACCESSIBILITY.md §4): dense inline controls on a mouse, full targets on touch.
 */
const TOUCH_FLOOR = 'min-h-11 [@media(hover:hover)]:min-h-0'

export default function CommentEditor({ initial = '', onSave, onCancel, onDraftChange }: {
  /** The starting text, or a function returning it (read once, at mount). */
  initial?: string | (() => string)
  onSave: (body: string) => void
  onCancel: () => void
  /** Every keystroke's text, so a caller that may remount the editor can hand it back as `initial`. */
  onDraftChange?: (body: string) => void
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
      <Textarea
        aria-label="Comment"
        placeholder="Leave a comment for Claude…"
        value={body}
        onChange={(e) => { setBody(e.target.value); onDraftChange?.(e.target.value) }}
        onKeyDown={onKeyDown}
        autoFocus
        autoGrow
        rows={2}
      />
      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" size="sm" className={TOUCH_FLOOR} onClick={onCancel}>Cancel</Button>
        <Button type="button" size="sm" className={TOUCH_FLOOR} disabled={!trimmed} onClick={save}>Save</Button>
      </div>
    </div>
  )
}

/** `line 3`, `lines 2–4`, plus ` (removed)` for the old side. */
function rangeLabel({ side, start, end }: CommentAnchor): string {
  return `${end > start ? `lines ${start}–${end}` : `line ${start}`}${side === 'old' ? ' (removed)' : ''}`
}

/**
 * What an outdated comment (or an open editor whose lines moved) was made on:
 * an "Outdated" tag, the range, optionally the file, and the quoted lines.
 */
export function StaleAnchor({ anchor, quote, stale, showPath }: {
  anchor: CommentAnchor; quote: readonly string[]; stale: boolean; showPath: boolean
}) {
  if (!stale && !showPath) return null
  return (
    <>
      <p className="mb-1 text-xs text-ink-muted">
        {stale && <span className="mr-1 rounded-sm bg-warning-wash px-1 text-ink">Outdated</span>}
        {showPath && <span className="mr-1 font-mono">{anchor.path}</span>}
        {rangeLabel(anchor)}
      </p>
      {stale && (
        <pre className="mb-1 whitespace-pre-wrap break-all border-l-2 border-edge pl-2 font-mono text-xs text-ink-muted">
          {quote.join('\n')}
        </pre>
      )}
    </>
  )
}

/**
 * A saved comment: its body with Edit / Delete. `stale` marks one whose lines no
 * longer match the diff (still sent, marked `(outdated)`). `showPath` names the
 * file, for a comment whose file left the diff entirely.
 */
export function CommentCard({ comment, stale = false, showPath = false, onEdit, onDelete }: {
  comment: LineComment
  stale?: boolean
  showPath?: boolean
  onEdit: (body: string) => void
  onDelete: () => void
}) {
  const [editing, setEditing] = useState(false)
  return (
    <article
      aria-label={`Comment on ${rangeLabel(comment.anchor)}`}
      className="rounded-md border border-edge bg-surface p-2 font-sans text-sm text-ink"
    >
      <StaleAnchor anchor={comment.anchor} quote={comment.quote} stale={stale} showPath={showPath} />
      {editing ? (
        <CommentEditor
          initial={comment.body}
          onSave={(body) => { onEdit(body); setEditing(false) }}
          onCancel={() => setEditing(false)}
        />
      ) : (
        <div className="flex items-start gap-2">
          <p className="min-w-0 flex-1 whitespace-pre-wrap break-words">{comment.body}</p>
          <Button type="button" variant="ghost" size="sm" className={TOUCH_FLOOR} onClick={() => setEditing(true)}>Edit</Button>
          <Button type="button" variant="ghost" size="sm" className={TOUCH_FLOOR} onClick={onDelete}>Delete</Button>
        </div>
      )}
    </article>
  )
}
