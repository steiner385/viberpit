import { api } from './client'
import type { DeployStatusResponse } from './types'

export function fetchDeployStatus(): Promise<DeployStatusResponse> {
  return api.get<DeployStatusResponse>('/deploy/status')
}

export function startDeploy(): Promise<{ started: boolean; reason?: string }> {
  return api.post<{ started: boolean; reason?: string }>('/deploy/start')
}
