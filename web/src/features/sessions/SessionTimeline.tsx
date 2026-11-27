/**
 * A session's lifecycle timeline (#947, spec 002 FR-301) on its review page.
 *
 * Provenance the transcript cannot show: why a queued session never started (each
 * typed attempt, including the one the drain re-enqueued) and that automation, not a
 * person, archived it. Collapsed by default — a `<details>` whose summary already
 * says how many entries there are — so it never pushes the transcript down.
 *
 * Lazy (`ReviewPage` imports it through `import()`), like `RecoveryRefsCard`.
 * Supplementary: while loading, on any error and with no events it renders nothing.
 * Detail values are untrusted text, never markup.
 */
import type { SessionTimelineEvent } from '@/api/types'
import { useSessionEvents } from '@/api/hooks'
import { isValidTimestamp } from '@/lib/relativeTime'
import { holdKindLabel } from '@/features/sessions/transcript/lostHold'

const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null)

/** One line per event. An unknown kind still shows, by its name. */
function describeEvent(e: SessionTimelineEvent): string {
  const d = e.detail ?? {}
  if (e.kind === 'spawn_failed') {
    const what = str(d.message) ?? str(d.reason) ?? 'unknown reason'
    const attempt = typeof d.attempt === 'number' ? ` (attempt ${d.attempt})` : ''
    return d.requeued === true
      ? `Failed to start${attempt}: ${what} — retrying`
      : `Failed to start${attempt}: ${what}`
  }
  if (e.kind === 'auto_archived') {
    const by = str(d.by)
    const reason = str(d.reason)
    return `Auto-archived${by ? ` by ${by}` : ''}${reason ? `: ${reason}` : ''}`
  }
  if (e.kind === 'hold_lost') {
    // The content itself is on the session's LostHoldBanner (FR-406); the line only
    // records that it happened.
    return `A pending ${holdKindLabel(d.kind)} was lost in a restart`
  }
  if (e.kind === 'rotated_account') {
    const from = str(d.from)
    const reason = str(d.reason)
    const attempt = typeof d.attempt === 'number' ? ` (attempt ${d.attempt})` : ''
    return `Rotated${from ? ` off ${from}` : ' account'}${reason ? `: ${reason}` : ''}${attempt}`
  }
  if (e.kind === 'delete_retry') {
    const strike = typeof d.strike === 'number' ? ` #${d.strike}` : ''
    const error = str(d.error)
    return `Delete retry${strike}${d.stuck === true ? ' — stuck' : ''}${error ? `: ${error}` : ''}`
  }
  if (e.kind === 'interrupted') {
    // Spec 002 FR-203: the host running the session stopped heartbeating and the
    // primary adopted the session off it.
    const from = str(d.fromStatus)
    const cause = d.kind === 'host_lost'
      ? `host ${str(d.host) ?? '(unknown)'} became unreachable`
      : str(d.kind)?.replace(/_/g, ' ') ?? 'interrupted'
    return `Interrupted: ${cause}${from ? ` (was ${from.replace(/_/g, ' ')})` : ''}`
  }
  if (e.kind === 'moved_host') {
    // FR-204: how the new host took it — resumed from its transcript, started over
    // with a continuity prompt, queued for a slot, left for its next prompt, or failed.
    const from = str(d.from)
    const to = str(d.to)
    const where = from && to ? ` from ${from} to ${to}` : to ? ` to ${to}` : ''
    const how: Record<string, string> = {
      transcript: 'conversation resumed',
      fresh: 'conversation could not be recovered — started over with context',
      queued: 'waiting for a slot there',
      deferred: 'resumes on its next prompt',
      failed: 'could not be resumed there',
    }
    const resume = str(d.resume)
    const outcome = resume ? how[resume] ?? resume : null
    return `Moved${where}${outcome ? `: ${outcome}` : ''}`
  }
  return e.kind.replace(/_/g, ' ')
}

export default function SessionTimeline({ sessionId }: { sessionId: string }) {
  const { data } = useSessionEvents(sessionId)
  // A body without an `events` array (an edge that predates the route) is as quiet
  // as an error.
  if (!data || !Array.isArray(data.events) || data.events.length === 0) return null
  const n = data.events.length

  return (
    <details
      data-testid="session-timeline"
      className="mx-4 mt-3 shrink-0 rounded-md border border-line px-3 py-2 text-sm md:mx-6"
    >
      <summary className="cursor-pointer text-ink-muted">
        Timeline ({n} {n === 1 ? 'event' : 'events'})
      </summary>
      <ol className="mt-2 max-h-48 space-y-1 overflow-y-auto overscroll-contain">
        {data.events.map((e) => (
          <li key={e.id} className="flex min-w-0 flex-wrap items-baseline gap-x-2">
            {isValidTimestamp(e.ts) && (
              <span className="text-xs text-ink-muted">{new Date(e.ts).toLocaleString()}</span>
            )}
            <span className="min-w-0 break-words text-ink">{describeEvent(e)}</span>
          </li>
        ))}
      </ol>
    </details>
  )
}
