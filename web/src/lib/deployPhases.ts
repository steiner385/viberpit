import type { DeployProgress } from '@/api/types'

export interface DeployPhaseMeta { key: string; label: string; description: string }

export const DEPLOY_PHASES: DeployPhaseMeta[] = [
  { key: 'preflight', label: 'Preflight', description: 'Checking CI and readiness' },
  { key: 'save', label: 'Save', description: 'Backing up live sessions' },
  { key: 'update', label: 'Update', description: 'Pulling latest code' },
  { key: 'report', label: 'Report', description: 'Recording the change' },
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

export function deployOutcome(deploy: DeployProgress | undefined): 'success' | 'failure' {
  const failed = !!deploy?.error
    || DEPLOY_PHASES.some(p => phaseState(deploy, p.key) === 'failed')
    || (deploy?.restore?.failed?.length ?? 0) > 0
  if (failed) return 'failure'
  return allPhasesDone(deploy) ? 'success' : 'failure'
}
