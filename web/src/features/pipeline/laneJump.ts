import { bucketPr, type Bucket } from '@/pipeline/StatusStrip'
import type { LaneStatus, PrView } from '@/pipeline/types'

/**
 * The repo a filter jump should FOCUS, or null to stay where the operator is.
 *
 * The header's lanes are cross-repo; the strip and the rows are the focused
 * repo's. A jump that only set the filter could land on a tile reading
 * "0 FAILED" beside a chip reading "1 red", the red row two sections down
 * under "(61 hidden)" (2026-09-30 Pipeline critique, P1). So the jump also
 * picks the repo: the focused one if it owns a row the lane is talking about,
 * else the first repo that does. "Talking about" is the lane's own word, not
 * just the bucket — the PR-CI chip's "1 red" counts `ci-failed` rows only,
 * while the Failed BUCKET also holds stuck-enqueue and group-failed, so a
 * repo with two stuck PRs must not swallow a jump aimed at the one red one.
 */
export function laneJumpRepo(
  laneId: string | null, status: LaneStatus | null, bucket: Bucket,
  repos: readonly { repo: string; prs: readonly PrView[] }[], focused: string | null,
): string | null {
  const attention = laneId === 'pr-ci' && status === 'red'
    ? (pr: PrView) => pr.stage.substate === 'ci-failed'
    : (pr: PrView) => bucketPr(pr) === bucket
  const owns = (r: { prs: readonly PrView[] }) => r.prs.some(attention)
  const current = repos.find((r) => r.repo === focused)
  if (current && owns(current)) return null
  return repos.find(owns)?.repo ?? null
}

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
