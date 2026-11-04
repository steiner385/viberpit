import { memo, useCallback, useEffect, useMemo, useRef, type Ref } from 'react'
import { defaultRangeExtractor, useVirtualizer, type Range } from '@tanstack/react-virtual'
import { cn } from '@/lib/utils'
import { useSessionFile } from '@/api/hooks'
import { ApiRequestError, apiErrorDetail } from '@/api/client'
import { HighlightedCode, LineNumber } from '../diff/DiffRenderer'
import { useHighlighter } from '../diff/highlightChunk'
import { TextAction } from './TextAction'
import { MAX_READ_BYTES, kib } from './types'

/** One line's height in px: the frame's `leading-5`. Rows never wrap, so it is exact. */
const ROW_PX = 20
/** Columns a tab advances: the browser's default `tab-size`, which the width estimate assumes. */
const TAB_COLS = 8
/**
 * Longest line highlighted, in chars. A minified bundle can be one 512 KiB line:
 * highlight.js on it would stall the tab when the row mounts, so a longer line
 * renders as plain text, and the width estimate counts no line past it.
 */
const MAX_HIGHLIGHT_LINE_CHARS = 5000

/**
 * The Files tab's read-only viewer (D-7): one worktree file, line-numbered and
 * syntax-highlighted through the diff renderer's shared pieces (`LineNumber`,
 * `HighlightedCode`, `useHighlighter` — the one lazy highlight.js chunk).
 *
 * A file is up to 512 KiB of text, so the lines are windowed
 * (@tanstack/react-virtual, as the transcript is): only the rows near the
 * viewport mount, and each is highlighted when it mounts — never the whole
 * file up front. Rows are a fixed height (no wrapping; the frame scrolls
 * sideways), so the window never remeasures.
 *
 * `line` (1-based, clamped to the file) is scrolled to the middle of the frame,
 * tinted, and focused, so a keyboard user lands on it; it stays mounted even
 * when scrolled out of the window (`rangeExtractor`), and stays in the tab order.
 */
export default function FileViewer({ sessionId, path, line }: {
  sessionId: string
  path: string
  line: number | null
}) {
  const { data, error, isError, refetch } = useSessionFile(sessionId, path)

  if (!data) {
    if (isError) return <FileError error={error} onRetry={() => void refetch()} />
    return <p className="p-4 text-sm text-ink-muted" aria-busy="true">Loading file…</p>
  }
  if (data.binary || data.text === null) {
    return <p className="p-4 text-sm text-ink-muted">Binary file — not shown</p>
  }
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      {data.truncated && <TruncatedNote limit={MAX_READ_BYTES} size={data.size} />}
      <FileLines key={data.path} path={data.path} text={data.text} line={line} />
    </div>
  )
}

/**
 * The viewer's frame for text already in hand: numbered, highlighted by `path`'s
 * extension, windowed. The Files tab feeds it a worktree read; the Context card's
 * memory viewer (D-13) feeds it a memory-file read.
 */
export function FileLines({ path, text, line }: { path: string; text: string; line: number | null }) {
  const lines = useMemo(() => splitLines(text), [text])
  const target = line == null ? null : Math.min(line, lines.length)
  const numWidth = `${String(lines.length).length}ch`
  const cols = useMemo(() => widestColumns(lines), [lines])
  const highlight = useHighlighter(path)
  const scrollRef = useRef<HTMLDivElement>(null)

  // The target row stays mounted wherever the window is, so it can hold focus
  // (and the tab stop) after the reader scrolls away from it.
  const rangeExtractor = useCallback((range: Range) => {
    const indexes = defaultRangeExtractor(range)
    if (target == null || indexes.includes(target - 1)) return indexes
    return [...indexes, target - 1].sort((a, b) => a - b)
  }, [target])

  // @tanstack/react-virtual returns uncacheable functions by design (see TranscriptView).
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: lines.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_PX,
    overscan: 20,
    rangeExtractor,
  })

  // Jump: scroll the line to the middle, then focus it — now if its row is
  // already mounted, else the moment it mounts (`focusOnMount`).
  const pendingFocus = useRef<number | null>(null)
  useEffect(() => {
    if (target == null) return
    virtualizer.scrollToIndex(target - 1, { align: 'center' })
    const row = scrollRef.current?.querySelector<HTMLElement>(`[data-line="${target}"]`)
    if (row) row.focus({ preventScroll: true })
    else pendingFocus.current = target
  }, [target, virtualizer])
  const focusOnMount = useCallback((el: HTMLDivElement | null) => {
    if (el && pendingFocus.current === target) {
      pendingFocus.current = null
      el.focus({ preventScroll: true })
    }
  }, [target])

  return (
    <div
      ref={scrollRef}
      role="region"
      aria-label={`Contents of ${path}`}
      // Focusable so a keyboard user can scroll the frame (it scrolls both ways).
      tabIndex={0}
      className={cn(
        'hljs min-h-0 min-w-0 flex-1 overflow-auto bg-canvas font-mono text-xs leading-5 text-ink',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent',
      )}
    >
      <div
        className="relative min-w-full"
        // Rows are absolutely placed, so the inner box is sized to the widest line
        // (monospace: digits + columns + the cells' padding).
        style={{ height: virtualizer.getTotalSize(), width: `calc(${String(lines.length).length + cols}ch + 1rem)` }}
      >
        {virtualizer.getVirtualItems().map((item) => {
          const n = item.index + 1
          return (
            <Row
              key={item.key}
              n={n}
              text={lines[item.index]}
              start={item.start}
              numWidth={numWidth}
              highlight={lines[item.index].length > MAX_HIGHLIGHT_LINE_CHARS ? null : highlight}
              target={n === target}
              rowRef={n === target ? focusOnMount : undefined}
            />
          )
        })}
      </div>
    </div>
  )
}

const Row = memo(function Row({ n, text, start, numWidth, highlight, target, rowRef }: {
  n: number
  text: string
  start: number
  numWidth: string
  highlight: ((text: string) => string) | null
  target: boolean
  rowRef?: Ref<HTMLDivElement>
}) {
  return (
    <div
      ref={rowRef}
      data-line={n}
      data-target={target ? '' : undefined}
      aria-current={target ? 'location' : undefined}
      tabIndex={target ? 0 : undefined}
      className={cn(
        'absolute left-0 top-0 flex h-5 w-full',
        // An accent box, not a fill: the frame is the highlight theme's own dark
        // background in both app themes, and a tint would cut the token colours'
        // contrast. It doubles as the focus indicator, so focus adds no outline.
        target && 'ring-2 ring-inset ring-accent focus:outline-none',
      )}
      style={{ transform: `translateY(${start}px)` }}
    >
      <LineNumber data-line-no="" n={n} width={numWidth} />
      <span className="whitespace-pre pr-2"><HighlightedCode text={text} highlight={highlight} /></span>
    </div>
  )
})

/** The note over a read cut at the server's cap: `limit` bytes shown, of `size` when the server says. */
export function TruncatedNote({ limit, size }: { limit: number; size?: number }) {
  return (
    <p role="note" className="shrink-0 border-b border-edge px-3 py-1 text-xs text-ink-muted">
      Large file — showing the first {kib(limit)}{size === undefined ? '' : ` of ${kib(size)}`}.
    </p>
  )
}

/**
 * The server's copy for an error it explains; this viewer's for the rest.
 * `notFound` replaces the 404 sentence for a reader whose 404 means something
 * narrower (the memory viewer's `MEMORY_FILE_UNKNOWN`).
 */
export function FileError({ error, onRetry, notFound }: { error: unknown; onRetry: () => void; notFound?: string }) {
  const status = error instanceof ApiRequestError ? error.status : null
  if (status === 400) return <p className="p-4 text-sm text-ink-muted">Can&apos;t open this path</p>
  if (status === 404) {
    return <p className="p-4 text-sm text-ink-muted">{notFound ?? 'File not found — it may have been moved or deleted.'}</p>
  }
  if (status === 409) {
    return (
      <p className="p-4 text-sm text-ink-muted">
        {apiErrorDetail(error).message ?? 'The session daemon cannot read files yet; try again after the next rollout.'}
      </p>
    )
  }
  return (
    <p className="p-4 text-sm text-ink-muted">
      Couldn&apos;t load this file.{' '}
      <TextAction onClick={onRetry}>Retry</TextAction>
    </p>
  )
}

/** The file's lines: a trailing newline ends the last line rather than starting an empty one; CRLF reads as LF. */
function splitLines(text: string): string[] {
  const lines = text.split('\n')
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop()
  for (let i = 0; i < lines.length; i++) if (lines[i].endsWith('\r')) lines[i] = lines[i].slice(0, -1)
  return lines
}

/**
 * The widest line in monospace columns, a tab counted at `TAB_COLS`, clamped to
 * `MAX_HIGHLIGHT_LINE_CHARS` columns: past that a line overflows the row (the frame
 * still scrolls to it) rather than sizing every row to a megabyte-wide box.
 */
function widestColumns(lines: string[]): number {
  let widest = 0
  for (const l of lines) {
    if (l.length >= MAX_HIGHLIGHT_LINE_CHARS) return MAX_HIGHLIGHT_LINE_CHARS
    let tabs = 0
    for (let i = l.indexOf('\t'); i !== -1; i = l.indexOf('\t', i + 1)) tabs++
    widest = Math.max(widest, l.length + tabs * (TAB_COLS - 1))
  }
  return Math.min(widest, MAX_HIGHLIGHT_LINE_CHARS)
}
