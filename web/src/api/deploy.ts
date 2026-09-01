import { api } from './client'
import type { DeployStatusResponse } from './types'

export function fetchDeployStatus(): Promise<DeployStatusResponse> {
  return api.get<DeployStatusResponse>('/deploy/status')
}

export function startDeploy(targetSha?: string): Promise<{ started: boolean; reason?: string }> {
  // No target keeps today's empty-body POST; a target deploys that exact sha (the
  // green frontier) instead of origin/master's red tip.
  return api.post<{ started: boolean; reason?: string }>(
    '/deploy/start', targetSha ? { targetSha } : undefined)
}
