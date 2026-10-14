// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { deployStatusRefetchInterval } from './hooks'
import type { DeployProgress, DeployStatusResponse } from './types'

const resp = (deploy: Partial<DeployProgress>) =>
  ({ pending: {} as DeployStatusResponse['pending'],
     deploy: { running: false, phases: {}, error: null, warnings: [], targetSha: '', ...deploy } }) as DeployStatusResponse

// #831: each deploy-status poll costs a daemon RPC; poll fast only while something moves.
describe('deployStatusRefetchInterval', () => {
  it('backs off to 30s when idle or before the first response', () => {
    expect(deployStatusRefetchInterval(undefined)).toBe(30_000)
    expect(deployStatusRefetchInterval(resp({}))).toBe(30_000)
    expect(deployStatusRefetchInterval(resp({ restore: { failed: [], gaveUp: ['s9'] } }))).toBe(30_000)
  })
  it('polls every 5s while a deploy runs', () => {
    expect(deployStatusRefetchInterval(resp({ running: true }))).toBe(5_000)
  })
  it('polls every 5s while sessions are still recovering, so the banner drains promptly', () => {
    expect(deployStatusRefetchInterval(resp({ restore: { failed: ['s1'] } }))).toBe(5_000)
  })
})
