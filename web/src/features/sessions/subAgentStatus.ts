// web/src/features/sessions/subAgentStatus.ts
//
// Single source of truth for a sub-agent status's colour/label presentation
// (#1471 carry-over 1). Originally two private copies — `SubAgentPage.tsx`'s
// `STATUS_TONE`/`STATUS_LABEL` and `SubAgentRows.tsx`'s untyped `DOT` map
// (raw palette, no `stopped` key) — consolidated here so every consumer
// (`SubAgentPage`, `SubAgentRows`, and 7e's `SubAgentList`/`SessionMeta`)
// agrees on both the tone and the label. `done` reads "completed": the wire
// event (`SubAgentEnded.status`) and the 2026-09-11 sub-agent spec both say
// "completed", not "done".
import type { SubAgentStatus } from '@/session/selectors'

/** Status → dot tone. The one place a sub-agent status becomes a colour. */
export const STATUS_TONE: Record<SubAgentStatus, string> = {
  running: 'bg-info animate-pulse motion-reduce:animate-none',
  done: 'bg-success',
  failed: 'bg-danger',
  stopped: 'bg-neutral',
}

export const STATUS_LABEL: Record<SubAgentStatus, string> = {
  running: 'running', done: 'completed', failed: 'failed', stopped: 'stopped',
}

/**
 * Status → dot tone, tolerant of an unrecognized wire value. `status` arrives
 * as a plain string off the wire, so an unknown value must still render as
 * something visible rather than an invisible dot — mirrors the FALLBACK idea
 * in `session/statusTier.ts` (no console noise here; this is a small, fixed
 * enum, not the session status vocabulary that warrants instrumentation).
 */
export function statusTone(status: string): string {
  return STATUS_TONE[status as SubAgentStatus] ?? 'bg-neutral'
}
