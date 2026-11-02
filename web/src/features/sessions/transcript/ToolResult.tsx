import { useMemo } from 'react'
import type { ToolKind } from '@/session/events'
import { cn } from '@/lib/utils'
import { DiffRenderer } from '../diff/DiffRenderer'
import { FileChangeList } from '../diff/FileChangeList'
import { fileChanges, toolInputToFileDiff } from '../diff/toolDiff'
import { FileContent } from './FileContent'
import { BashResult } from './BashResult'
import { TodoList } from './TodoList'
import { SearchResult } from './SearchResult'
import { SubagentResult } from './SubagentResult'
import { WebResult } from './WebResult'
import { GenericResult } from './GenericResult'

interface Props {
  kind: ToolKind
  ok: boolean
  /** The tool's call arguments — Edit/Write diffs are reconstructed from here. */
  input: Record<string, unknown>
  payload: Record<string, unknown>
  /** Tool name (e.g. Grep, WebFetch, mcp__github__list_prs) for renderer context. */
  name?: string
}

export function ToolResult({ kind, ok, input, payload, name }: Props) {
  switch (kind) {
    case 'edit':
    case 'write':
      return <EditResult input={input} ok={ok} />
    case 'read':
      return <FileContent payload={payload} ok={ok} />
    case 'bash':
      return <BashResult payload={payload} ok={ok} />
    case 'todo':
      return <TodoList payload={payload} />
    case 'search':
      return <SearchResult payload={payload} ok={ok} name={name} />
    case 'task':
      return <SubagentResult payload={payload} ok={ok} />
    case 'web':
      return <WebResult input={input} payload={payload} ok={ok} name={name} />
    case 'mcp':
    default:
      return <GenericResult payload={payload} ok={ok} />
  }
}

/** Inline diffs stop here until "show all" — a 2,000-line Write would otherwise bury the transcript. */
const MAX_LINES = 200

/**
 * An Edit/Write/MultiEdit result. The diff is reconstructed from the call's
 * `input` — the result payload only carries the success/error message. A Codex
 * change carries no text, so it lists the changed files instead. A MultiEdit's
 * `edits[]` is not rendered (a pre-existing gap): only top-level
 * `old_string`/`new_string`/`content` are read.
 */
function EditResult({ input, ok }: { input: Record<string, unknown>; ok: boolean }) {
  const diff = useMemo(() => toolInputToFileDiff(input), [input])
  const changes = useMemo(() => (diff ? [] : fileChanges(input)), [diff, input])
  if (!diff && changes.length === 0) return null
  const frame = cn(
    'max-h-64 md:max-h-96',
    // A failed edit/write didn't apply — flag the attempted change in red.
    !ok && 'border border-danger',
  )
  if (diff) return <DiffRenderer file={diff} view="unified" maxLines={MAX_LINES} className={frame} />
  return (
    <div className={cn('overflow-auto rounded bg-canvas text-xs font-mono', frame)}>
      <FileChangeList changes={changes} />
    </div>
  )
}
