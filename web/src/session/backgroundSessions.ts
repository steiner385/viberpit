// web/src/session/backgroundSessions.ts
//
// The foreground/background split of the LIVE sessions list.
//
// ViberPit spawns routine maintenance sessions of its own — `pm map`,
// `pm classify`, `pr-doctor fix`, control-band diagnoses — and they used to sit
// in the Active list beside the work the operator is actually steering. They
// are important but not attention-worthy: they run unattended in full-auto and
// self-archive on completion. The list keeps them in a Background scope and
// lifts one into the foreground only while it needs a human.
//
// Two facts decide the split, and only two:
//
//   · `workClass` — published by the server from the automation registry
//     (`server/automations/base.py:work_class_of`). The browser holds NO list
//     of which origins are housekeeping; `backlog-runner` and the SDLC origins
//     are `development` and never come here. A session with no `workClass` at
//     all (an older server, a mutation response that omitted it) is
//     foreground: nothing is hidden on a guess.
//   · the triage tier — the SAME `triageTier` read the card pill and the group
//     header use, so a session can never be "quiet" here while its card is
//     pulsing red in the Active list.
//
// `QUIET_TIERS` is a closed allowlist rather than a list of tiers that lift:
// a tier added later lifts by default, which is the fail-visible direction.
import type { Session } from '@/api/types'
import type { StatusTier } from './statusTier'
import { triageTier } from './triageTier'

/** Tiers at which a maintenance session stays out of the way: an agent is on
 *  it (`busy`), it is parked on a slot or a Monitor (`waiting`), or it has
 *  declared itself finished (`complete`). Everything else — a permission card,
 *  a question, a parked model upgrade, a failure, a red PR, an unreachable
 *  host, or a turn that ended without completing (`your-turn`, which for an
 *  unattended session means it stalled) — lifts it into the foreground. */
export const QUIET_TIERS: ReadonlySet<StatusTier> = new Set<StatusTier>(['busy', 'waiting', 'complete'])

/** True when the live list should keep this session in the Background scope. */
export function isBackgroundSession(session: Session): boolean {
  return session.workClass === 'maintenance' && QUIET_TIERS.has(triageTier(session))
}

export interface AttentionPartition {
  /** Human sessions, development-class automation sessions, and every
   *  maintenance session that currently needs a human — the Active scope. */
  foreground: Session[]
  /** Quiet maintenance sessions — the Background scope. */
  background: Session[]
}

/** One pass, input order preserved on both sides. */
export function partitionByAttention(sessions: readonly Session[]): AttentionPartition {
  const foreground: Session[] = []
  const background: Session[] = []
  for (const s of sessions) (isBackgroundSession(s) ? background : foreground).push(s)
  return { foreground, background }
}
