/**
 * AssistantHistoryList — the drawer's "History" popover: the caller's own
 * archived assistant threads (`GET /assistant/history`, server-capped at 20,
 * most-recently-archived first), one row per thread. Clicking a row resumes it
 * (`POST /assistant/session/{id}/resume`) and closes the popover.
 *
 * This component does NOT itself switch `AssistantSheet`'s active session: the
 * resume mutation's `onSuccess` invalidates the `assistant/status` query (see
 * `useResumeAssistantSession` in `@/api/assistant`), and `AssistantSheet` already
 * derives its live `sessionId` from that query — the same mechanism a "New chat"
 * reset already relies on. Duplicating that switch here would be a second,
 * driftable copy of "what session is live".
 *
 * Row markup is deliberately plain rather than reusing `ArchivedSessionCard` /
 * `GroupedList` / `SelectableRow`: those are built for the archived-sessions
 * PAGE (multi-select, swipe actions, bulk delete, repo/account chips) and none
 * of that applies to a short resume-only list inside a drawer popover. Only the
 * relative-time formatter (`relTime`/`isValidTimestamp`) is shared, so an
 * "Nd ago" label never drifts from the rest of the app.
 */
import { useAssistantHistory, useResumeAssistantSession } from '@/api/assistant'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { useNow } from '@/hooks/useNow'
import { Dialog } from '@/ui/Dialog'

export interface AssistantHistoryListProps {
  open: boolean
  onClose: () => void
}

export function AssistantHistoryList({ open, onClose }: AssistantHistoryListProps) {
  const history = useAssistantHistory(open)
  const resume = useResumeAssistantSession()
  // 60s, not a per-second tick: same reasoning as ArchivedSessionCard — a
  // history row's timestamp is at least a "new chat" old, never sub-minute.
  const now = useNow(60_000)

  function handleResume(id: string) {
    resume.mutate(id, { onSuccess: () => onClose() })
  }

  return (
    <Dialog open={open} onClose={onClose} title="Assistant history" size="sm">
      {history.isLoading && (
        <p className="text-sm text-ink-muted">Loading…</p>
      )}
      {!history.isLoading && (history.data?.length ?? 0) === 0 && (
        <p className="text-sm text-ink-muted">No previous conversations yet.</p>
      )}
      {(history.data?.length ?? 0) > 0 && (
        <ul role="list" className="-mx-1 max-h-80 divide-y divide-edge overflow-y-auto">
          {history.data!.map((entry) => (
            <li key={entry.id}>
              <button
                type="button"
                onClick={() => handleResume(entry.id)}
                disabled={resume.isPending}
                className="flex w-full flex-col items-start gap-0.5 rounded-md px-1 py-2 text-left hover:bg-surface-raised disabled:opacity-50"
              >
                <span className="line-clamp-2 text-sm text-ink">
                  {entry.snippet || 'Untitled conversation'}
                </span>
                {isValidTimestamp(entry.archivedAt) && (
                  <span className="text-xs text-ink-muted tabular-nums">
                    {relTime(entry.archivedAt, now || undefined)}
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
      {resume.isError && (
        <p role="alert" className="mt-2 text-xs text-danger">Couldn’t resume that conversation.</p>
      )}
    </Dialog>
  )
}
