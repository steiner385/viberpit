import { useState } from 'react'
import CommentEditor, { CommentCard } from './CommentEditor'
import LineCommentGutter, { GutterSpacer } from './LineCommentGutter'
import { afterDelete, gutterButton, useReturnFocus } from './returnFocus'
import { newCommentId, sortComments, type LineComment } from './reviewComments'

/**
 * The plan card's "What should change?" view (D-5): the plan text as numbered
 * lines, each one a comment target (`Comment on step line N`), with the saved
 * step comments and the open editor under their line. One line per comment —
 * no shift-range. Presentational over `comments`; the card owns the draft and
 * composes it with `composePlanFeedback`.
 *
 * Height-capped and scrollable like the plain request text it replaces, so a
 * long plan never pushes the card's answer buttons off screen. Focus returns to
 * the line's gutter button when its editor closes, and after a delete to the
 * next card's Edit button, else that line's gutter button.
 *
 * Lazily loaded by `PermissionCard` only when comment mode opens: the plan
 * card is first-paint, so the gutter/editor must stay out of the entry chunk.
 */
export default function PlanStepLines({ text, comments, onChange }: {
  text: string
  /** Kept in line order, so the composed feedback reads top to bottom. */
  comments: readonly LineComment[]
  onChange: (next: LineComment[]) => void
}) {
  const [open, setOpen] = useState<number | null>(null)
  const { root, returnFocus } = useReturnFocus<HTMLDivElement>()
  const lines = text.split('\n')
  const close = (n: number) => {
    returnFocus((r) => gutterButton(r, 'new', n))
    setOpen(null)
  }
  return (
    <div ref={root} aria-label="Plan" role="group"
         className="max-h-48 overflow-y-auto overscroll-contain rounded border border-warning/40 bg-surface px-1.5 py-2 font-mono text-xs leading-5 text-ink">
      {lines.map((line, i) => {
        const n = i + 1
        const blank = line.trim() === ''
        const saved = comments.filter((c) => c.anchor.start === n)
        return (
          <div key={n}>
            {/* `data-diff-row` is the gutter's hover-reveal hook (shared with the diff). */}
            <div data-diff-row className="flex items-start gap-1.5">
              {blank ? <GutterSpacer /> : (
                <LineCommentGutter line={n} side="new" noun="step line" selected={open === n}
                                   onClick={() => setOpen(n)} />
              )}
              <span aria-hidden="true" className="w-6 shrink-0 select-none text-right text-ink-muted">{n}</span>
              <span className="min-w-0 flex-1 whitespace-pre-wrap [overflow-wrap:anywhere]">{line}</span>
            </div>
            {(saved.length > 0 || open === n) && (
              <div className="my-1 ml-2 flex min-w-0 flex-col gap-1">
                {saved.map((c) => (
                  <CommentCard key={c.id} comment={c}
                    onEdit={(body) => onChange(comments.map((x) => (x.id === c.id ? { ...x, body } : x)))}
                    onDelete={() => {
                      returnFocus(afterDelete(root.current, c.id, (r) => gutterButton(r, 'new', n)))
                      onChange(comments.filter((x) => x.id !== c.id))
                    }} />
                ))}
                {open === n && (
                  <CommentEditor
                    key={n}
                    onCancel={() => close(n)}
                    onSave={(body) => {
                      onChange(sortComments([...comments, {
                        id: newCommentId(),
                        anchor: { path: null, side: 'new', start: n, end: n },
                        quote: [line.trim()],
                        body,
                      }]))
                      close(n)
                    }}
                  />
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
