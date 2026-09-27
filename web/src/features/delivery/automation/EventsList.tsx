/**
 * The Events tab: an automation's activity feed, newest first. The tab is the
 * heading and the page scrolls — no inner scroll box, no "Activity" label
 * restating the tab (docs/PAGE-TABS.md §3, §7). Times are the reader's local
 * clock with the date once the entry is not from today; the ISO timestamp is
 * the `<time>`'s machine value and its title.
 */
import { useAutomationEvents } from '@/api/hooks'
import { formatEventTime } from '@/lib/formatBlockTime'

export function EventsList({ automationId, onOpenSession }: {
  automationId: string
  onOpenSession: (id: string) => void
}) {
  const events = useAutomationEvents(automationId)
  const list = events.data ?? []
  if (list.length === 0) return <p className="text-sm text-ink-muted">No activity yet.</p>
  return (
    <ol className="space-y-1 text-sm" aria-label="Events">
      {list.map((e) => {
        const { short, full } = formatEventTime(e.ts)
        return (
          <li key={`${e.ts}-${e.sessionId ?? ''}`} className="flex gap-2">
            <time dateTime={e.ts} title={full} className="text-ink-muted tabular-nums shrink-0">{short}</time>
            {e.sessionId
              ? (
                // No `link` Button variant exists (ui/Button.tsx) — a plain
                // button on tokens is the documented fallback for an inline
                // text link inside a list row.
                <button
                  type="button"
                  onClick={() => onOpenSession(e.sessionId!)}
                  className="text-accent hover:underline text-left"
                >
                  {e.summary}
                </button>
              )
              : <span>{e.summary}</span>}
          </li>
        )
      })}
    </ol>
  )
}
