import type { DeployProgress, DeployHostProgress } from '@/api/types'

export interface DeployPhaseMeta { key: string; label: string; description: string }

export const DEPLOY_PHASES: DeployPhaseMeta[] = [
  { key: 'preflight', label: 'Preflight', description: 'Checking CI and readiness' },
  { key: 'save', label: 'Save', description: 'Backing up live sessions' },
  { key: 'update', label: 'Update', description: 'Pulling latest code' },
  { key: 'report', label: 'Report', description: 'Recording the change' },
  { key: 'runners', label: 'Runners', description: 'Updating session hosts' },
  { key: 'restart', label: 'Restart', description: 'Restarting service' },
  { key: 'resume', label: 'Resume', description: 'Restoring your sessions' },
]

type PhaseState = 'pending' | 'running' | 'done' | 'failed'

export function phaseState(deploy: DeployProgress | undefined, key: string): PhaseState {
  const s = deploy?.phases?.[key]
  return (s === 'running' || s === 'done' || s === 'failed') ? s : 'pending'
}

export function deployProgress(deploy: DeployProgress | undefined): { done: number; total: number } {
  const done = DEPLOY_PHASES.filter(p => phaseState(deploy, p.key) === 'done').length
  return { done, total: DEPLOY_PHASES.length }
}

export function allPhasesDone(deploy: DeployProgress | undefined): boolean {
  return DEPLOY_PHASES.every(p => phaseState(deploy, p.key) === 'done')
}

/** Per-fleet-runner rows for the `runners` phase, sorted by host name. Empty
 *  (never a placeholder error) when the deploy is a no-op with no hosts to update. */
export function hostRows(deploy: DeployProgress | undefined): Array<[string, DeployHostProgress]> {
  return Object.entries(deploy?.hosts ?? {}).sort(([a], [b]) => a.localeCompare(b))
}

/** Statuses whose turn the daemon drains before it restarts (mirrors
 *  `server/sessions/status.py` MID_TURN_STATUSES). */
export const MID_TURN_STATUSES: ReadonlySet<string> = new Set(['in_progress', 'condensing', 'retrying'])

/** What a deploy does to the sessions that are live right now — the one
 *  sentence the confirm gate exists to say (2026-09-30 Settings critique, P1).
 *  Restart semantics per `deploy/README.md`: the daemon drains turns in flight
 *  before it restarts, then autopilot resumes every session; a session it
 *  cannot resume is reported, never lost silently. */
export function deployConsequence(liveCount: number, midTurn: number): string {
  if (liveCount === 0) return 'Restarts the server and the session daemon. No session is live right now.'
  const live = liveCount === 1 ? '1 live session pauses' : `${liveCount} live sessions pause`
  const turns = midTurn === 0 ? ''
    : midTurn === 1 ? ' The turn in progress finishes first (up to a few minutes).'
      : ` The ${midTurn} turns in progress finish first (up to a few minutes).`
  return `Restarts the server and the session daemon. ${live} while it restarts, then autopilot resumes them.${turns} A session that cannot be resumed is reported here and on the banner.`
}

export type DeployOutcome = 'success' | 'failure' | 'idle'

export function deployOutcome(deploy: DeployProgress | undefined): DeployOutcome {
  // A lost session (restore.failed) is NOT a deploy failure: the backend keeps the
  // RESUME phase DONE, never sets error, and never rolls back — the upgrade completed.
  // Those sessions are surfaced as a warning on the success screen instead.
  const failed = !!deploy?.error
    || DEPLOY_PHASES.some(p => phaseState(deploy, p.key) === 'failed')
  if (failed) return 'failure'
  if (allPhasesDone(deploy)) return 'success'
  // Not running, no error, and NO phase ever started: a fresh controller process with
  // no deploy in flight (e.g. reborn from its post-deploy self-restart, #865). That is
  // "nothing happening", never "Upgrade failed".
  const started = DEPLOY_PHASES.some(p => phaseState(deploy, p.key) !== 'pending')
  if (!started && !deploy?.running) return 'idle'
  return 'failure'
}
