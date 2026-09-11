// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { laneJumpTarget } from './laneJump'

describe('laneJumpTarget', () => {
  it('maps pr-ci red → failed filter', () => {
    expect(laneJumpTarget('pr-ci', 'red')).toEqual({ kind: 'filter', bucket: 'failed' })
  })
  it('maps pr-ci non-red → running filter', () => {
    expect(laneJumpTarget('pr-ci', 'green')).toEqual({ kind: 'filter', bucket: 'running' })
    expect(laneJumpTarget('pr-ci', 'amber')).toEqual({ kind: 'filter', bucket: 'running' })
    expect(laneJumpTarget('pr-ci', null)).toEqual({ kind: 'filter', bucket: 'running' })
  })
  it('maps merge-queue → queued filter', () => {
    expect(laneJumpTarget('merge-queue', 'amber')).toEqual({ kind: 'filter', bucket: 'queued' })
  })
  it('maps deploy → deploy filter', () => {
    expect(laneJumpTarget('deploy', 'green')).toEqual({ kind: 'filter', bucket: 'deploy' })
  })
  it('maps failures → failed filter', () => {
    expect(laneJumpTarget('failures', 'red')).toEqual({ kind: 'filter', bucket: 'failed' })
  })
  it('maps main / scheduled / cost / null → ci-health', () => {
    expect(laneJumpTarget('main', 'green')).toEqual({ kind: 'ci-health' })
    expect(laneJumpTarget('scheduled', 'amber')).toEqual({ kind: 'ci-health' })
    expect(laneJumpTarget('cost', null)).toEqual({ kind: 'ci-health' })
    expect(laneJumpTarget(null, null)).toEqual({ kind: 'ci-health' })
  })
})
