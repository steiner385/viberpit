/**
 * The Events tab: an automation's activity feed. Moved from
 * `AutomationsTab.tsx`'s `AutomationLiveCard` Activity section — same rows,
 * always fetched (the tab being open replaces the old expand/collapse gate).
 */
import { useAutomationEvents } from '@/api/hooks'

export function EventsList({ automationId, onOpenSession }: {
  automationId: string
  onOpenSession: (id: string) => void
}) {
  const events = useAutomationEvents(automationId)
  const list = events.data ?? []
  return (
    <section>
      <h4 className="text-xs font-semibold uppercase text-ink-muted mb-2">Activity</h4>
      {list.length === 0
        ? <p className="text-sm text-ink-muted">No activity yet.</p>
        : (
          <div className="max-h-40 overflow-y-auto rounded-md border border-edge p-2">
            <ul className="space-y-1 text-sm">
              {list.map((e) => (
                <li key={`${e.ts}-${e.sessionId ?? ''}`} className="flex gap-2">
                  <span className="text-ink-muted tabular-nums shrink-0">{e.ts.slice(11, 19)}</span>
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
              ))}
            </ul>
          </div>
        )}
    </section>
  )
}
