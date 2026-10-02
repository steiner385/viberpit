import { formatElapsed } from './elapsed'
import type { BackgroundWorkKind, BackgroundWorkView } from '@/session/selectors'

export const KIND_LABEL: Record<BackgroundWorkKind, string> = {
  shell: 'shell', monitor: 'monitor', terminal: 'terminal', workflow: 'workflow', task: 'task',
}

/** "2 shells · 1 monitor · wakeup in 1m 0s" — the one phrase the chip, the card pill
 *  and the Waiting… title all read, so they can never disagree. Null when nothing runs. */
export function backgroundWorkSummary(view: BackgroundWorkView, now: number): string | null {
  const counts = new Map<BackgroundWorkKind, number>()
  for (const r of view.rows) if (r.status === 'running') counts.set(r.kind, (counts.get(r.kind) ?? 0) + 1)
  const parts = [...counts].map(([k, n]) => `${n} ${KIND_LABEL[k]}${n === 1 ? '' : 's'}`)
  if (view.wakeup) parts.push(view.wakeupAt != null ? `wakeup in ${formatElapsed(Math.max(0, view.wakeupAt - now))}` : 'wakeup armed')
  return parts.length ? parts.join(' · ') : null
}
