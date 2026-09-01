import { describe, it, expect } from 'vitest'
import {
  DEPLOY_PHASES, phaseState, allPhasesDone, deployProgress, deployOutcome,
} from './deployPhases'
import type { DeployProgress } from '@/api/types'

const D = (over: Partial<DeployProgress>): DeployProgress => ({
  running: false, phases: {}, error: null, warnings: [], targetSha: 'x', ...over,
})

describe('deployPhases', () => {
  it('lists the 6 phases in deploy order', () => {
    expect(DEPLOY_PHASES.map(p => p.key)).toEqual(
      ['preflight', 'save', 'update', 'report', 'restart', 'resume'])
    expect(DEPLOY_PHASES.every(p => p.label && p.description)).toBe(true)
  })

  it('phaseState defaults missing phases to pending', () => {
    expect(phaseState(D({ phases: { save: 'done' } }), 'save')).toBe('done')
    expect(phaseState(D({ phases: { save: 'done' } }), 'update')).toBe('pending')
    expect(phaseState(undefined, 'preflight')).toBe('pending')
  })

  it('deployProgress counts done phases out of total', () => {
    expect(deployProgress(D({ phases: { preflight: 'done', save: 'done' } })))
      .toEqual({ done: 2, total: 6 })
  })

  it('allPhasesDone is true only when every phase is done', () => {
    const all = Object.fromEntries(DEPLOY_PHASES.map(p => [p.key, 'done']))
    expect(allPhasesDone(D({ phases: all }))).toBe(true)
    expect(allPhasesDone(D({ phases: { preflight: 'done' } }))).toBe(false)
  })

  it('deployOutcome is failure on error or a failed phase, not on a lost session', () => {
    const all = Object.fromEntries(DEPLOY_PHASES.map(p => [p.key, 'done']))
    expect(deployOutcome(D({ phases: all }))).toBe('success')
    expect(deployOutcome(D({ phases: all, error: 'boom' }))).toBe('failure')
    expect(deployOutcome(D({ phases: { ...all, restart: 'failed' } }))).toBe('failure')
    // A completed deploy that couldn't resume some sessions still succeeded — the
    // backend treats lost sessions as a warning, not a rollback. Surface them as a
    // warning on the success screen instead of a red "Upgrade failed".
    expect(deployOutcome(D({ phases: all, restore: { restored: [], failed: ['s1'] } })))
      .toBe('success')
    expect(deployOutcome(D({ phases: { preflight: 'done' } }))).toBe('failure') // incomplete
  })

  it('deployOutcome is idle for an empty, non-running, error-free status', () => {
    // A controller process replaced mid-poll (its post-deploy self-restart, #865)
    // starts with phases:{} and running:false. That is "no deploy in progress" —
    // reading it as failure showed a red "Upgrade failed" after a completed upgrade.
    expect(deployOutcome(D({}))).toBe('idle')
    expect(deployOutcome(undefined)).toBe('idle')
    // ...but an error or any started phase still classifies as before.
    expect(deployOutcome(D({ error: 'boom' }))).toBe('failure')
    expect(deployOutcome(D({ phases: { preflight: 'running' } }))).toBe('failure')
  })
})
