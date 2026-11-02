import { cn } from '@/lib/utils'
import type { FileChange } from './toolDiff'

/** Per-file change kinds a Codex `Edit` reports (`server/codex/schema.py`). */
const CHANGE_VERBS: Record<string, string> = {
  add: 'added',
  update: 'updated',
  delete: 'deleted',
}

/**
 * A Codex file change: WHICH files changed and how, never the text either side
 * — the app-server does not send old/new content, so there is no diff to draw.
 * Show what the engine actually told us instead of an empty diff box.
 */
export function FileChangeList({ changes }: { changes: FileChange[] }) {
  return (
    <ul className="p-2">
      {changes.map((c) => (
        <li key={c.path} className="flex gap-2">
          <span className={cn('flex-shrink-0', c.kind === 'delete' ? 'text-danger' : 'text-success')}>
            {c.kind === 'delete' ? '-' : c.kind === 'add' ? '+' : '~'}
          </span>
          <span className="break-all">{c.path}</span>
          {c.kind && (
            <span className="flex-shrink-0 text-ink-muted">
              {CHANGE_VERBS[c.kind] ?? c.kind}
            </span>
          )}
        </li>
      ))}
    </ul>
  )
}
