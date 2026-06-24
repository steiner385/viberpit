import { formatElapsed } from './elapsed'
import type { SubAgent } from '@/session/selectors'

/**
 * How long a sub-agent has run, formatted (e.g. "2m 30s"):
 *  - running → live elapsed since it was spawned (ticks via `now`)
 *  - done/failed → frozen total runtime (spawn → result)
 * Returns null when the timestamps needed aren't available (e.g. a replayed
 * transcript whose events carried no `ts`), so the row simply omits a duration.
 */
export function agentDuration(a: SubAgent, now: number): string | null {
  if (a.startedAt == null) return null
  if (a.status === 'running') return formatElapsed(Math.max(0, now - a.startedAt))
  if (a.endedAt == null) return null
  return formatElapsed(Math.max(0, a.endedAt - a.startedAt))
}
