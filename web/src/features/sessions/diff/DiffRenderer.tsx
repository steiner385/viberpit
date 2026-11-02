import { Fragment, memo, useEffect, useMemo, useState, type HTMLAttributes, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { loadDiffHighlight, loadedDiffHighlight } from './highlightChunk'
import type { DiffHunk, DiffLine, FileDiff } from './types'

export type DiffViewMode = 'unified' | 'split'

interface Props {
  file: FileDiff
  view: DiffViewMode
  /** Render at most this many diff lines, then a "show all" button. Unset = everything. */
  maxLines?: number
  /** Controlled "show all" (with `onShowAll`) for a caller that must know what is drawn — `CommentableDiff`. Unset = the renderer's own. */
  showAll?: boolean
  onShowAll?: () => void
  /** Extra classes on the frame — the frame is the scroll container, so a height cap goes here. */
  className?: string
  /** Slots for an overlay such as line comments (`review/CommentableDiff`). Unset = the plain diff. */
  decorations?: DiffDecorations
}

/**
 * Overlay slots, kept generic so the comment UI ships in the Changes tab's chunk,
 * not with the transcript's inline diffs (which never pass them).
 */
export interface DiffDecorations {
  /** Before a cell's numbers. `side` is the split column, or null in unified (one cell per line). */
  gutter: (line: DiffLine | null, side: 'old' | 'new' | null) => ReactNode
  /** Under a row, given its line (unified) or its old and new cells (split; null = empty cell). */
  below: (a: DiffLine | null, b?: DiffLine | null) => ReactNode
}

/**
 * One file's diff — the shared renderer behind both the transcript's inline
 * Edit/Write results and the Changes pane.
 *
 * Unified draws each line once with old/new numbers and a +/- gutter. Split
 * pairs each run of deletions with the run of additions that follows it, row by
 * row, so a changed line sits beside its replacement; the side a run is short
 * on gets an empty cell. Context lines appear on both sides.
 *
 * Line numbers (`LineNumber`) and hunk headers are dimmed with a
 * translucent text colour, not `opacity` — see `LineNumber` for why.
 *
 * Colours come from the app's one highlight.js theme: the frame is `.hljs` and
 * add/del rows are `.hljs-addition` / `.hljs-deletion`, so diff and code-block
 * colours always agree. Syntax highlighting is loaded on demand (see
 * `diffHighlight.ts`); until it arrives, and for files with no known language,
 * the lines are plain text.
 */
export const DiffRenderer = memo(function DiffRenderer({
  file, view, maxLines, showAll: shownAll, onShowAll, className, decorations: deco,
}: Props) {
  const [ownShowAll, setOwnShowAll] = useState(false)
  const showAll = shownAll ?? ownShowAll
  const highlight = useHighlighter(file.path)

  const total = useMemo(() => file.hunks.reduce((n, h) => n + h.lines.length, 0), [file])
  const hunks = useMemo(
    () => (showAll || maxLines == null ? file.hunks : limitHunks(file.hunks, maxLines)),
    [file, showAll, maxLines],
  )
  const anchored = useMemo(() => isAnchored(file.hunks), [file])
  const numWidth = useMemo(() => `${String(maxLineNo(file.hunks)).length}ch`, [file])
  const shown = hunks.reduce((n, h) => n + h.lines.length, 0)

  const body = useMemo(() => {
    const cell = (line: DiffLine | null, side: 'old' | 'new' | null) => (
      <LineCell line={line} side={side} anchored={anchored} numWidth={numWidth} highlight={highlight} split={view === 'split'}
                lead={deco?.gutter(line, side)} />
    )
    return hunks.map((h, i) => (
      <div key={i}>
        {anchored && (
          <div className="px-2 py-0.5 text-current/60 select-none" data-hunk-header>
            {hunkHeader(h)}
          </div>
        )}
        {view === 'unified'
          ? h.lines.map((l, j) => (
            <Fragment key={j}>
              <div data-diff-row data-kind={l.kind} className={cn('flex min-h-5', rowClass(l.kind))}>
                {cell(l, null)}
              </div>
              {deco?.below(l)}
            </Fragment>
          ))
          : splitRows(h.lines).map(([left, right], j) => (
            <Fragment key={j}>
              <div data-diff-row className="grid grid-cols-2">
                {cell(left, 'old')}
                {cell(right, 'new')}
              </div>
              {deco?.below(left, right)}
            </Fragment>
          ))}
      </div>
    ))
  }, [hunks, view, anchored, numWidth, highlight, deco])

  if (file.binary || file.tooLarge) {
    return (
      <div className={cn('hljs rounded bg-canvas p-2 text-xs text-ink', className)}>
        {file.binary ? 'Binary file — no text diff.' : 'Diff too large to display.'}
      </div>
    )
  }

  return (
    <div className={cn('hljs overflow-auto rounded bg-canvas text-xs leading-5 font-mono text-ink', className)}>
      <div className={cn('min-w-full py-1', view === 'unified' && 'w-max')}>{body}</div>
      {shown < total && (
        <button
          type="button"
          onClick={() => { setOwnShowAll(true); onShowAll?.() }}
          className="px-3 py-1 text-xs text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          show all {total} lines
        </button>
      )}
    </div>
  )
})

interface CellProps {
  line: DiffLine | null
  /** Split view: which column this cell is. Unified: null (one cell per row, both numbers). */
  side: 'old' | 'new' | null
  anchored: boolean
  numWidth: string
  highlight: ((text: string) => string) | null
  split: boolean
  /** A decoration's gutter, before the numbers. */
  lead: ReactNode
}

function LineCell({ line, side, anchored, numWidth, highlight, split, lead }: CellProps) {
  const num = (attr: 'data-old-no' | 'data-new-no', n: number | null | undefined) =>
    anchored && <LineNumber {...{ [attr]: '' }} n={n} width={numWidth} />
  const content = line && <HighlightedCode text={line.text} highlight={highlight} />
  const sign = line && (
    <span data-sign className="shrink-0 select-none pr-1">{line.kind === 'add' ? '+' : line.kind === 'del' ? '-' : ' '}</span>
  )
  const code = (
    <span className={cn('min-w-0 pr-2', split ? 'whitespace-pre-wrap break-all' : 'whitespace-pre')}>{content}</span>
  )

  if (side === null) {
    return <>{lead}{num('data-old-no', line?.oldNo)}{num('data-new-no', line?.newNo)}{sign}{code}</>
  }
  return (
    <div
      data-side={side}
      data-kind={line ? line.kind : 'empty'}
      className={cn('flex min-h-5 min-w-0', line && rowClass(line.kind), side === 'new' && 'border-l border-edge')}
    >
      {lead}
      {line && num(side === 'old' ? 'data-old-no' : 'data-new-no', side === 'old' ? line.oldNo : line.newNo)}
      {sign}
      {code}
    </div>
  )
}

/** Fallback washes sit in Tailwind's layer, so the unlayered theme classes win once the theme CSS is loaded. */
function rowClass(kind: DiffLine['kind']): string | undefined {
  if (kind === 'add') return 'hljs-addition bg-success-wash'
  if (kind === 'del') return 'hljs-deletion bg-danger-wash'
  return undefined
}

function hunkHeader(h: DiffHunk): string {
  return `@@ -${h.oldStart},${h.oldLines} +${h.newStart},${h.newLines} @@${h.header ? ` ${h.header}` : ''}`
}

/** Line numbers exist only for a diff taken against a real file (not a tool call's fragment). */
function isAnchored(hunks: DiffHunk[]): boolean {
  return hunks.some((h) => h.lines.some((l) => l.oldNo != null || l.newNo != null))
}

function maxLineNo(hunks: DiffHunk[]): number {
  let max = 0
  for (const h of hunks) for (const l of h.lines) max = Math.max(max, l.oldNo ?? 0, l.newNo ?? 0)
  return max
}

/** The first `max` lines across hunks, dropping hunks that start past the cut. */
function limitHunks(hunks: DiffHunk[], max: number): DiffHunk[] {
  const out: DiffHunk[] = []
  let left = max
  for (const h of hunks) {
    if (left <= 0) break
    out.push(h.lines.length <= left ? h : { ...h, lines: h.lines.slice(0, left) })
    left -= h.lines.length
  }
  return out
}

/**
 * Split-view rows: context lines on both sides; each run of deletions paired
 * row by row with the run of additions right after it, the shorter run padded
 * with null (an empty cell).
 */
function splitRows(lines: DiffLine[]): Array<[DiffLine | null, DiffLine | null]> {
  const rows: Array<[DiffLine | null, DiffLine | null]> = []
  let i = 0
  while (i < lines.length) {
    if (lines[i].kind === 'context') {
      rows.push([lines[i], lines[i]])
      i++
      continue
    }
    const dels: DiffLine[] = []
    const adds: DiffLine[] = []
    while (i < lines.length && lines[i].kind === 'del') dels.push(lines[i++])
    while (i < lines.length && lines[i].kind === 'add') adds.push(lines[i++])
    for (let k = 0; k < Math.max(dels.length, adds.length); k++) rows.push([dels[k] ?? null, adds[k] ?? null])
  }
  return rows
}

/*
 * Shared with the Files tab's viewer (D-7), so a line looks and is coloured the
 * same in both.
 */

/** One line's source, as highlight.js markup when there is a highlighter, else plain text. */
export function HighlightedCode({ text, highlight }: { text: string; highlight: ((text: string) => string) | null }) {
  // highlight.js escapes the source; its only markup is its own token spans.
  if (highlight) return <span dangerouslySetInnerHTML={{ __html: highlight(text) }} />
  return <>{text}</>
}

/**
 * A line-number cell with room for `width` (`<digits>ch`) of digits — the
 * min-width adds the `px-1` padding back, as the box is `border-box` and three
 * digits would otherwise spill past a two-digit column. Dimmed with a translucent
 * text colour, not `opacity` — an opacity layer is a stacking context, and would
 * paint over a touch gutter's hit area where it extends over the numbers
 * (`review/LineCommentGutter`). Extra attributes (`data-*`) pass through.
 */
export function LineNumber({ n, width, className, ...rest }: {
  n: number | null | undefined
  width: string
} & HTMLAttributes<HTMLSpanElement>) {
  return (
    <span {...rest} className={cn('shrink-0 select-none px-1 text-right text-current/60', className)} style={{ minWidth: `calc(${width} + 0.5rem)` }}>
      {n ?? ''}
    </span>
  )
}

/**
 * The line highlighter for `path`, or null while it loads / for plain text.
 * Shared by `DiffRenderer` and the Files tab's viewer (D-7), so both colour a
 * file the same way and load highlight.js through the one lazy chunk.
 */
// A hook beside the components: the Files tab's viewer (D-7) imports these from
// this module, so the diff renderer's chunk is not split in two for it.
// eslint-disable-next-line react-refresh/only-export-components
export function useHighlighter(path: string): ((text: string) => string) | null {
  const [mod, setMod] = useState(loadedDiffHighlight)
  useEffect(() => {
    if (!path || mod) return
    let live = true
    void loadDiffHighlight().then((m) => { if (live && m) setMod(m) })
    return () => { live = false }
  }, [path, mod])
  return useMemo(() => (path && mod ? mod.highlighterFor(path) : null), [path, mod])
}
