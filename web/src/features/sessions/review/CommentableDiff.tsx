import { useCallback, useMemo, useRef, useState, type ComponentProps, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { DiffRenderer, type DiffDecorations } from '../diff/DiffRenderer'
import type { DiffHunk } from '../diff/types'
import CommentEditor, { CommentCard, StaleAnchor } from './CommentEditor'
import { anchorMatches, lineKey, lineKeys, lineNo, onSide, quoteOf, type Side } from './diffAnchors'
import LineCommentGutter, { GutterSpacer } from './LineCommentGutter'
import { afterDelete, fileOrTabFallback, gutterButton, useReturnFocus } from './returnFocus'
import type { CommentAnchor, LineComment } from './reviewComments'

/**
 * The range being commented, snapshotted when clicked: its side, lines and
 * quoted text. Save uses only this — never the (possibly refetched) diff — so a
 * refetch while the editor is open can neither crash Save nor change the quote.
 * `hunk` is the hunk index at click time, used only to scope a shift-click.
 */
type Selection = { side: Side; hunk: number; from: number; start: number; end: number; quote: string[] }

interface Props extends Omit<ComponentProps<typeof DiffRenderer>, 'decorations' | 'showAll' | 'onShowAll'> {
  /** This file's comments (anchor.path === file.path). Keep the array stable while it is unchanged. */
  comments: readonly LineComment[]
  onAddComment: (anchor: CommentAnchor, quote: string[], body: string) => void
  onEditComment: (id: string, body: string) => void
  onDeleteComment: (id: string) => void
}

/**
 * `DiffRenderer` with line comments (D-5), through its decoration slots.
 *
 * Every numbered line gets a gutter button. A click starts a range; a shift-click
 * on the same side of the same hunk extends it, anywhere else starts a new one.
 * The editor opens under the range's last line. A saved comment sits under its
 * last line while its lines still read as quoted; one whose lines changed, or
 * that is past the render cap, goes to the top of the file (changed = "Outdated").
 * An open editor whose lines changed under a refetch moves there too, keeping
 * its text.
 *
 * Side: in split view the column (left = `old`, right = `new`); in unified the
 * line's own — `old` for a removed line, `new` for an added or context line.
 *
 * It owns the renderer's "show all" state, so what counts as past the cap is
 * the renderer's own view of it: once expanded, every comment sits inline.
 *
 * Focus returns to the range's gutter button when the editor closes (Save or
 * Cancel), and after a delete to the next card's Edit button, else the deleted
 * comment's gutter button, else (a stale or past-cap card) the file's header,
 * else the Changes tab itself.
 *
 * Lives in the Changes tab's chunk; the transcript's inline diffs use the bare
 * renderer and never load it.
 */
export default function CommentableDiff({ comments, onAddComment, onEditComment, onDeleteComment, ...diff }: Props) {
  const { file, maxLines } = diff
  const [sel, setSel] = useState<Selection | null>(null)
  const [showAll, setShowAll] = useState(false)
  const { root, returnFocus } = useReturnFocus<HTMLDivElement>()
  // The editor's text, kept per keystroke without re-rendering: the editor remounts
  // when its row moves (a refetch), and a shift-extend keeps what was typed. Read
  // only by the editor's lazy initial state, never during this render.
  const typed = useRef('')

  const shown = useMemo(
    () => (maxLines == null || showAll ? null : shownKeys(file.hunks, maxLines)), [file, maxLines, showAll])

  // Comments a row can hold, by `side:line`; the rest go to the top.
  const { placed, top } = useMemo(() => {
    const placed = new Map<string, LineComment[]>()
    const top: Array<{ c: LineComment; stale: boolean }> = []
    for (const c of comments) {
      const key = lineKey(c.anchor.side, c.anchor.end)
      const fresh = anchorMatches(file.hunks, c.anchor, c.quote)
      if (fresh && (shown === null || shown.has(key))) placed.set(key, [...(placed.get(key) ?? []), c])
      else top.push({ c, stale: !fresh })
    }
    return { placed, top }
  }, [comments, file, shown])

  // Which hunk a line is in — the renderer's capped hunks reuse these line objects.
  const hunkOf = useMemo(() => new Map(file.hunks.flatMap((h, i) => h.lines.map((l) => [l, i] as const))), [file])

  // The open editor's anchor, from the snapshot; it stays in its row only while that row still reads as quoted.
  const editorAnchor = useMemo<CommentAnchor | null>(
    () => sel && { path: file.path, side: sel.side, start: sel.start, end: sel.end }, [sel, file.path])
  const editorInPlace = useMemo(
    () => sel !== null && editorAnchor !== null && anchorMatches(file.hunks, editorAnchor, sel.quote)
      && (shown === null || shown.has(lineKey(sel.side, sel.end))),
    [sel, editorAnchor, file, shown])

  const card = useCallback((c: LineComment, stale = false) => (
    <CommentCard key={c.id} comment={c} stale={stale}
                 onEdit={(b) => onEditComment(c.id, b)}
                 onDelete={() => {
                   returnFocus(afterDelete(root.current, c.id,
                     (r) => gutterButton(r, c.anchor.side, c.anchor.end) ?? fileOrTabFallback(r)))
                   onDeleteComment(c.id)
                 }} />
  ), [onEditComment, onDeleteComment, root, returnFocus])

  const editor = useMemo(() => {
    if (!sel || !editorAnchor) return null
    const close = () => {
      returnFocus((r) => gutterButton(r, sel.side, sel.end))
      setSel(null)
    }
    return (
      <CommentEditor
        key="editor"
        initial={() => typed.current}
        onDraftChange={(t) => { typed.current = t }}
        onCancel={close}
        onSave={(body) => {
          onAddComment(editorAnchor, sel.quote, body)
          close()
        }}
      />
    )
  }, [sel, editorAnchor, onAddComment, returnFocus])

  const decorations = useMemo<DiffDecorations>(() => ({
    gutter: (line, column) => {
      const side = column ?? (line?.kind === 'del' ? 'old' : 'new')
      const n = line && onSide(line, side) ? lineNo(line, side) : null
      if (!line || n == null) return <GutterSpacer />
      const hunk = hunkOf.get(line) ?? -1
      return (
        <LineCommentGutter
          line={n}
          side={side}
          selected={sel?.side === side && sel.hunk === hunk && n >= sel.start && n <= sel.end}
          onClick={(e) => {
            const extend = e.shiftKey && sel && sel.side === side && sel.hunk === hunk
            const [start, end] = extend ? [Math.min(sel.from, n), Math.max(sel.from, n)] : [n, n]
            setSel({
              side, hunk, from: extend ? sel.from : n, start, end,
              quote: quoteOf(file.hunks[hunk], side, start, end),
            })
            // A fresh range on another row starts empty; a shift-extend, or a click on the
            // row the editor is already under, keeps what was typed.
            const sameRow = sel?.side === side && sel.hunk === hunk && sel.end === end
            if (!extend && !sameRow) typed.current = ''
          }}
        />
      )
    },
    below: (...cells) => {
      // A split context row passes one line as both cells: dedupe its keys.
      const keys = [...new Set(cells.flatMap((l) => (l ? lineKeys(l) : [])))]
      const saved = keys.flatMap((k) => placed.get(k) ?? [])
      const editing = sel !== null && editorInPlace && keys.includes(lineKey(sel.side, sel.end))
      if (saved.length === 0 && !editing) return null
      return <Slot>{saved.map((c) => card(c))}{editing && editor}</Slot>
    },
  }), [sel, placed, hunkOf, editorInPlace, file, card, editor])

  const strayEditor = sel && editorAnchor && !editorInPlace && (
    <div className="rounded-md border border-edge bg-surface p-2 text-sm text-ink">
      <StaleAnchor anchor={editorAnchor} quote={sel.quote} stale showPath={false} />
      {editor}
    </div>
  )

  // `@container` on the scrolling frame: a slot inside it sizes to the frame's
  // visible width (`cqw`), not to the wide unified rows it scrolls.
  return (
    <div ref={root} className="min-w-0">
      {(top.length > 0 || strayEditor) && <Slot>{top.map(({ c, stale }) => card(c, stale))}{strayEditor}</Slot>}
      <DiffRenderer {...diff} className={cn('@container', diff.className)} decorations={decorations}
                    showAll={showAll} onShowAll={() => setShowAll(true)} />
    </div>
  )
}

/**
 * Comments and the editor under a row (or atop the file). Sticky to the frame's
 * left edge and capped at the frame's visible width (`100cqw` — a `%` would
 * resolve against the `w-max` unified rows, and a long comment would widen
 * them past a phone's screen), so a wide diff scrolled sideways never carries
 * them out of view or clips them.
 */
function Slot({ children }: { children: ReactNode }) {
  return (
    <div data-comment-slot className="sticky left-0 w-full min-w-0 max-w-[min(100cqw,40rem)] space-y-1 px-2 py-1">
      {children}
    </div>
  )
}

/** The keys of the first `max` lines — what the renderer draws under its cap. */
function shownKeys(hunks: DiffHunk[], max: number): Set<string> {
  return new Set(hunks.flatMap((h) => h.lines).slice(0, max).flatMap(lineKeys))
}
