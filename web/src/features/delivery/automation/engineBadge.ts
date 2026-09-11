import type { Automation } from '@/api/automations'

/** Label for the engine this automation actually launches on, or null when
 *  there is nothing to say (Phase 7). Moved from `components/AutomationCard.tsx`
 *  (finish-line row 4, R8); the automation page header renders it as a Badge.
 *
 *  Deliberately silent for a `claude_only` origin: every origin is claude_only
 *  until it passes a Codex acceptance suite, so a badge on all of them would be
 *  noise. It reads the CONFIG rather than the policy, because `codex_allowed` is
 *  only permission — an origin can hold it and still be running Claude. */
export function engineBadge(a: Automation): string | null {
  if (!a.enginePolicy || a.enginePolicy === 'claude_only') return null
  const selected = a.config?.engine ?? a.configSchema.find((f) => f.key === 'engine')?.default
  return selected === 'codex' ? 'Codex' : 'Claude'
}
