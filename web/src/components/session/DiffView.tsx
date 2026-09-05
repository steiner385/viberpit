import { useState } from 'react'
import { cn } from '@/lib/utils'
import { buildLines } from '@/lib/toolContent'
import { changedPaths } from '@/session/toolSummary'

// `input` is the tool's call arguments (Edit: old_string/new_string, Write:
// content). The result payload carries only the success/error message, so the
// diff MUST be reconstructed from the input.
interface Props { input: Record<string, unknown>; ok?: boolean }

const MAX_LINES = 200

/** Per-file change kinds a Codex `Edit` reports (`server/codex/schema.py`). */
const CHANGE_VERBS: Record<string, string> = {
  add: 'added',
  update: 'updated',
  delete: 'deleted',
}

export function DiffView({ input, ok = true }: Props) {
  const [showAll, setShowAll] = useState(false)
  const allLines = buildLines(input)
  // A Codex file change reports WHICH files changed and how, never the text
  // either side — the app-server does not send old/new content. `buildLines`
  // therefore returns nothing, and rendering its empty result was an empty
  // bordered box that claimed a diff existed. Show what the engine actually
  // told us instead of a blank stand-in for what it did not.
  if (allLines.length === 0) {
    const changes = fileChanges(input)
    if (changes.length > 0) return <FileChangeList changes={changes} ok={ok} />
  }
  const visible = showAll ? allLines : allLines.slice(0, MAX_LINES)
  const hidden = allLines.length - visible.length

  return (
    <div
      className={cn(
        'max-h-64 overflow-auto rounded bg-gray-50 dark:bg-gray-900 text-xs font-mono md:max-h-96',
        // A failed edit/write didn't apply — flag the attempted diff in red.
        !ok && 'border border-red-400 dark:border-red-700',
      )}
    >
      <pre className="p-2">
        {visible.map((l, i) => (
          <div
            key={i}
            className={cn(
              l.prefix === '-'
                ? 'text-red-700 dark:text-red-400'
                : 'text-green-700 dark:text-green-400',
            )}
          >
            {l.prefix} {l.text}
          </div>
        ))}
      </pre>
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setShowAll(true)}
          className="px-3 py-1 text-xs text-blue-600 dark:text-blue-400 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
        >
          show all {allLines.length} lines
        </button>
      )}
    </div>
  )
}

interface FileChange {
  path: string
  kind: string | null
}

/** `changes: [{path, kind}]` as a typed list; empty for any other shape. */
function fileChanges(input: Record<string, unknown>): FileChange[] {
  const paths = changedPaths(input.changes)
  if (paths.length === 0) return []
  const raw = Array.isArray(input.changes) ? input.changes : []
  return paths.map((path, i) => {
    const entry = raw[i]
    const kind =
      entry != null && typeof entry === 'object' && 'kind' in entry
        ? (entry as { kind: unknown }).kind
        : null
    return { path, kind: typeof kind === 'string' ? kind : null }
  })
}

function FileChangeList({ changes, ok }: { changes: FileChange[]; ok: boolean }) {
  return (
    <ul
      className={cn(
        'max-h-64 overflow-auto rounded bg-gray-50 dark:bg-gray-900 p-2 text-xs font-mono md:max-h-96',
        !ok && 'border border-red-400 dark:border-red-700',
      )}
    >
      {changes.map((c) => (
        <li key={c.path} className="flex gap-2">
          <span
            className={cn(
              'flex-shrink-0',
              c.kind === 'delete'
                ? 'text-red-700 dark:text-red-400'
                : 'text-green-700 dark:text-green-400',
            )}
          >
            {c.kind === 'delete' ? '-' : c.kind === 'add' ? '+' : '~'}
          </span>
          <span className="break-all">{c.path}</span>
          {c.kind && (
            <span className="flex-shrink-0 text-gray-500 dark:text-gray-400">
              {CHANGE_VERBS[c.kind] ?? c.kind}
            </span>
          )}
        </li>
      ))}
    </ul>
  )
}
