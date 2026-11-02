import { useMemo, useState, type ComponentProps, type ReactNode } from 'react'
import { DiffRenderer, type DiffDecorations } from '../diff/DiffRenderer'
import type { DiffHunk, DiffLine } from '../diff/types'
import CommentEditor, { CommentCard } from './CommentEditor'
import LineCommentGutter from './LineCommentGutter'
import type { CommentAnchor, LineComment } from './reviewComments'

type Side = CommentAnchor['side']

/** The range being commented: one side of one hunk, `from` the first click. */
type Selection = { side: Side; hunk: number; from: number; start: number; end: number }

interface Props extends Omit<ComponentProps<typeof DiffRenderer>, 'decorations'> {
  /** This file's comments (anchor.path === file.path). */
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
 * last line while that line still reads as quoted; one whose lines changed, or
 * that is past the render cap, goes to the top of the file (changed = "Outdated").
 *
 * Side: in split view the column (left = `old`, right = `new`); in unified the
 * line's own — `old` for a removed line, `new` for an added or context line.
 *
 * Lives in the Changes tab's chunk; the transcript's inline diffs use the bare
 * renderer and never load it.
 */
export default function CommentableDiff({ comments, onAddComment, onEditComment, onDeleteComment, ...diff }: Props) {
  const { file } = diff
  const [sel, setSel] = useState<Selection | null>(null)

  const card = (c: LineComment, stale = false) => (
    <CommentCard key={c.id} comment={c} stale={stale}
                 onEdit={(b) => onEditComment(c.id, b)} onDelete={() => onDeleteComment(c.id)} />
  )

  // Comments a row can hold, by `side:line`; the rest go to the top.
  const { placed, top } = useMemo(() => {
    const placed = new Map<string, LineComment[]>()
    const top: Array<{ c: LineComment; stale: boolean }> = []
    const shown = diff.maxLines == null ? null : shownKeys(file.hunks, diff.maxLines)
    for (const c of comments) {
      const key = `${c.anchor.side}:${c.anchor.end}`
      const fresh = file.hunks.some((h) => matchesQuote(h, c))
      if (fresh && (shown === null || shown.has(key))) placed.set(key, [...(placed.get(key) ?? []), c])
      else top.push({ c, stale: !fresh })
    }
    return { placed, top }
  }, [comments, file, diff.maxLines])

  // Which hunk a line is in — the renderer's capped hunks reuse these line objects.
  const hunkOf = useMemo(() => new Map(file.hunks.flatMap((h, i) => h.lines.map((l) => [l, i] as const))), [file])

  const decorations: DiffDecorations = {
    gutter: (line, column) => {
      const side = column ?? (line?.kind === 'del' ? 'old' : 'new')
      const n = line && onSide(line, side) ? lineNo(line, side) : null
      if (!line || n == null) return <span className="w-5 shrink-0" />
      const hunk = hunkOf.get(line) ?? -1
      return (
        <LineCommentGutter
          line={n}
          selected={sel?.side === side && sel.hunk === hunk && n >= sel.start && n <= sel.end}
          onClick={(e) => setSel((p) => (e.shiftKey && p && p.side === side && p.hunk === hunk
            ? { ...p, start: Math.min(p.from, n), end: Math.max(p.from, n) }
            : { side, hunk, from: n, start: n, end: n }))}
        />
      )
    },
    below: (...cells) => {
      // A split context row passes one line as both cells: dedupe its keys.
      const keys = [...new Set(cells.flatMap((l) => (l ? lineKeys(l) : [])))]
      const saved = keys.flatMap((k) => placed.get(k) ?? [])
      const editing = sel !== null && keys.includes(`${sel.side}:${sel.end}`)
      if (saved.length === 0 && !editing) return null
      return (
        <Slot>
          {saved.map((c) => card(c))}
          {editing && (
            <CommentEditor
              onCancel={() => setSel(null)}
              onSave={(body) => {
                const { side, start, end, hunk } = sel
                onAddComment({ path: file.path, side, start, end }, quoteOf(file.hunks[hunk], side, start, end), body)
                setSel(null)
              }}
            />
          )}
        </Slot>
      )
    },
  }

  return (
    <>
      {top.length > 0 && <Slot>{top.map(({ c, stale }) => card(c, stale))}</Slot>}
      <DiffRenderer {...diff} decorations={decorations} />
    </>
  )
}

/**
 * Comments and the editor under a row (or atop the file). Sticky to the frame's
 * left edge and capped in width, so a wide unified diff scrolled sideways never
 * carries them out of view.
 */
function Slot({ children }: { children: ReactNode }) {
  return <div data-comment-slot className="sticky left-0 w-full max-w-[min(100%,40rem)] space-y-1 px-2 py-1">{children}</div>
}

/** Whether `line` exists on `side`: the new side has context + adds, the old side context + dels. */
function onSide(line: DiffLine, side: Side): boolean {
  return side === 'old' ? line.kind !== 'add' : line.kind !== 'del'
}

function lineNo(line: DiffLine, side: Side): number | null {
  return side === 'old' ? line.oldNo : line.newNo
}

/** The `side:number` keys a line answers to — both sides for a context line. */
function lineKeys(line: DiffLine): string[] {
  return (['old', 'new'] as const).filter((s) => onSide(line, s)).map((s) => `${s}:${lineNo(line, s)}`)
}

/** The keys of the first `max` lines — what the renderer draws under its cap. */
function shownKeys(hunks: DiffHunk[], max: number): Set<string> {
  return new Set(hunks.flatMap((h) => h.lines).slice(0, max).flatMap(lineKeys))
}

/** The text of `side`'s lines `start..end` in one hunk — a comment's quote. */
function quoteOf(hunk: DiffHunk, side: Side, start: number, end: number): string[] {
  return hunk.lines
    .filter((l) => {
      const n = onSide(l, side) ? lineNo(l, side) : null
      return n != null && n >= start && n <= end
    })
    .map((l) => l.text)
}

/** A comment still anchors when its lines exist in this hunk with the text it quoted. */
function matchesQuote(hunk: DiffHunk, c: LineComment): boolean {
  const { side, start, end } = c.anchor
  const now = quoteOf(hunk, side, start, end)
  return now.length === end - start + 1 && now.length === c.quote.length && now.every((t, k) => t === c.quote[k])
}
