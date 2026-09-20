import type { CapacityReadout } from '@/api/capacity'

export interface GovernorNotice { tone: 'warning' | 'info'; title: string; body: string }

/**
 * The three silent ways the governor can stop governing (spec 2026-09-20 C18):
 * the kill switch, the mode, and plan staleness — folded into ONE sentence for
 * the intent row, most severe first. Null means "act mode, live plan": nothing
 * to say.
 */
export function governorNotice(
  r: Pick<CapacityReadout, 'governorMode' | 'killSwitch' | 'plan' | 'planLive'>,
): GovernorNotice | null {
  if (!r.killSwitch) {
    return {
      tone: 'warning',
      title: 'Governor kill switch is off',
      body: 'VIBERPIT_CAPACITY_GOVERNOR_ENABLED is false on the server: no plan is ever applied (and, unless the mode is off, plans are still computed and shown).',
    }
  }
  if (r.governorMode === 'off') {
    return {
      tone: 'info',
      title: 'Governor is off',
      body: 'No plan is computed; the runner uses each product’s stored pin and the stored runner ceiling.',
    }
  }
  if (r.governorMode === 'monitor') {
    return {
      tone: 'info',
      title: 'Governor is in monitor mode',
      body: 'The plan below is computed every tick but not applied; switch to act to let it set the caps.',
    }
  }
  if (r.plan === null) {
    return { tone: 'info', title: 'No plan yet', body: 'The governor has not ticked since it was enabled; stored caps apply until it does.' }
  }
  if (r.plan.applied && !r.planLive) {
    return {
      tone: 'warning',
      title: 'The last plan is stale',
      body: 'It is older than 10 minutes, so the runner is using stored caps until the governor ticks again.',
    }
  }
  return null
}
