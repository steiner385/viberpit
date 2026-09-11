import type { Bucket } from '@/pipeline/StatusStrip'
import type { LaneStatus } from '@/pipeline/types'

/** Lane chip → richest existing surface (plotroom replica spec §5): the Pipeline
 *  status filter for the PR-flow lanes, Delivery's CI-health scope for the rest
 *  until richer targets exist. Pure so the page can unit-test the mapping. */
export function laneJumpTarget(laneId: string | null, status: LaneStatus | null):
  { kind: 'filter'; bucket: Bucket | null } | { kind: 'ci-health' } {
  switch (laneId) {
    case 'pr-ci': return { kind: 'filter', bucket: status === 'red' ? 'failed' : 'running' }
    case 'merge-queue': return { kind: 'filter', bucket: 'queued' }
    case 'deploy': return { kind: 'filter', bucket: 'deploy' }
    case 'failures': return { kind: 'filter', bucket: 'failed' }
    default: return { kind: 'ci-health' } // main / scheduled / cost / null
  }
}
