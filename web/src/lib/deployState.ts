// web/src/lib/deployState.ts
import { usePendingDeploy, useDeployStatus } from '@/api/hooks'
import type { PendingDeploy, DeployProgress } from '@/api/types'

/**
 * Everything "is there a deploy to worry about, and can it run?" — derived ONCE.
 *
 * This lived inside `DeployPanel` while the banner was the only surface. It moved
 * out when Settings › Deploy became the second one: the green-frontier rule, the
 * stale-preflight rule and the `unrun` label are subtle enough that two copies
 * would disagree within a release, and they would disagree in the direction that
 * matters — one surface offering a Deploy the other says is impossible.
 *
 * The derivation is a pure function of the two API shapes, so it is testable
 * without rendering and callable from `useNavBadges` (which needs `hasFailure`
 * only) without pulling in a component.
 */

// A CI verdict rendered for a human. Only "unrun" needs translating: it means the
// commit's CI run was cancelled before any job started, so no check will ever arrive.
// Showing that as "pending" tells the operator to wait for something that is not
// coming — the one verdict on this banner that never resolves on its own.
const CI_VERDICT_LABELS: Record<string, string> = { unrun: 'never ran' }
export const ciLabel = (verdict: string): string => CI_VERDICT_LABELS[verdict] ?? verdict

export interface DeployState {
  behind: boolean
  running: boolean
  commits: PendingDeploy['commits']
  count: number
  /** What the Deploy button can act on ("green" whenever anything pending is deployable). */
  ci: string
  /** The tip's own verdict — differs from `ci` exactly when a green frontier is in play. */
  tipCi: string
  /** The tip is red/pending but an older pending commit is green. */
  partial: boolean
  deployableCount: number
  canDeploy: boolean
  /** Argument for `start()`: an explicit sha for a partial deploy, `undefined` for the tip. */
  deployTarget: string | undefined
  /** Why Deploy is unavailable, or how much a partial deploy covers. Empty when plainly deployable. */
  reason: string
  showError: boolean
  error: string | null
  /** Sessions autopilot is still retrying. */
  failed: string[]
  /** Sessions autopilot gave up on — these need a human. */
  gaveUp: string[]
  hasFailure: boolean
  /** Nothing pending and nothing wrong: the banner renders null on this. */
  idle: boolean
}

export function deriveDeployState(
  pending: PendingDeploy | undefined,
  deploy: DeployProgress | undefined,
): DeployState {
  const behind = pending?.behind ?? false
  const running = deploy?.running ?? false
  const phases = deploy?.phases ?? {}
  // `failed` = still being auto-recovered by autopilot; `gaveUp` = recovery capped
  // out (needs a human). Both are the daemon's LIVE set — the notice clears itself as
  // sessions come back, so no manual "open to retry" is needed for the transient case.
  const failed = deploy?.restore?.failed ?? []
  const gaveUp = deploy?.restore?.gaveUp ?? []
  const unresolved = failed.length + gaveUp.length
  const commits = pending?.commits ?? []
  const count = commits.length
  const ci = pending?.ci ?? 'unavailable'
  const tipCi = pending?.tipCi ?? ci
  const deployableSha = pending?.deployableSha ?? ''
  const deployableCount = pending?.deployableCount ?? 0
  // Green frontier: the tip is red/pending but an older pending commit is green — the
  // Deploy button targets that sha and the surface says honestly how much it covers.
  const partial = ci === 'green' && tipCi !== 'green' && !!deployableSha && deployableCount > 0

  // A prior deploy that failed only at the preflight CI gate is stale once CI is green again.
  const preflightStale = !running && phases.preflight === 'failed' && ci === 'green'
  // A past deploy error is actionable only while commits still await deploy. A restart/health-gate
  // failure rolls back to the previous version, so once the checkout is caught up or ahead of origin
  // (behind=false) the error is pure history — hide it. Session-resume failures are independent (a
  // successful daemon deploy leaves behind=false yet may have lost sessions) and always surface.
  const showError = !running && !preflightStale && behind && !!deploy?.error
  const hasFailure = showError || (!running && unresolved > 0)

  const canDeploy = behind && !running && ci === 'green'
  const reason = running ? 'deploying…'
    : partial ? `${deployableCount} of ${count} deployable (tip CI ${ciLabel(tipCi)})`
    : ci !== 'green' ? `CI ${ciLabel(ci)}` : ''

  return {
    behind, running, commits, count, ci, tipCi, partial, deployableCount,
    canDeploy, deployTarget: partial ? deployableSha : undefined, reason,
    showError, error: deploy?.error ?? null, failed, gaveUp, hasFailure,
    idle: !behind && !hasFailure,
  }
}

/** `deriveDeployState` over the live queries. Both surfaces read the same poll (one query key). */
export function useDeployState(): DeployState {
  const { data: pending } = usePendingDeploy()
  const { data: deploy } = useDeployStatus()
  return deriveDeployState(pending, deploy)
}
