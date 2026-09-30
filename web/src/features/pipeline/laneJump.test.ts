// @vitest-environment node
import { describe, expect, it } from 'vitest'
import type { PrView } from '@/pipeline/types'
import { laneJumpRepo, laneJumpTarget } from './laneJump'

const at = (stage: string, substate: string | null = null): PrView =>
  ({ stage: { stage, substate, percent: null, etaSeconds: null, etaRangeSeconds: null, overdue: false } } as unknown as PrView)

// 2026-09-30 Pipeline critique, P1: the lanes are cross-repo, the strip and
// rows are the focused repo's; a jump has to land on the repo that owns the row
// the lane is talking about.
describe('laneJumpRepo', () => {
  const repos = [
    { repo: 'o/a', prs: [at('ci'), at('ready', 'stuck-enqueue')] },   // a stuck PR: Failed BUCKET, not red
    { repo: 'o/b', prs: [at('parked', 'ci-failed')] },                // the one red row
    { repo: 'o/c', prs: [at('queue')] },
  ]

  it('for a red PR-CI chip, follows the ci-failed row — not just any Failed-bucket row', () => {
    expect(laneJumpRepo('pr-ci', 'red', 'failed', repos, 'o/a')).toBe('o/b')
    expect(laneJumpRepo('pr-ci', 'red', 'failed', repos, null)).toBe('o/b')
  })

  it('stays put (null) when the focused repo already owns such a row', () => {
    expect(laneJumpRepo('pr-ci', 'red', 'failed', repos, 'o/b')).toBeNull()
    expect(laneJumpRepo('failures', 'red', 'failed', repos, 'o/a')).toBeNull() // stuck-enqueue IS in the Failed bucket
    expect(laneJumpRepo('merge-queue', 'amber', 'queued', repos, 'o/c')).toBeNull()
  })

  it('for other lanes, follows the bucket', () => {
    expect(laneJumpRepo('merge-queue', 'amber', 'queued', repos, 'o/a')).toBe('o/c')
    expect(laneJumpRepo('pr-ci', 'green', 'running', repos, 'o/b')).toBe('o/a')
  })

  it('returns null when no repo owns such a row', () => {
    expect(laneJumpRepo('deploy', 'green', 'deploy', repos, 'o/a')).toBeNull()
  })
})

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
