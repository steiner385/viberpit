// web/src/session/stageBucket.ts
import type { PipelinePr, StageModel, StageNode } from '@/api/types'

export type StageBucket = 'ci' | 'ready' | 'queue' | 'deploy' | 'merged'

export const STAGE_BUCKETS: StageBucket[] = ['ci', 'ready', 'queue', 'merged', 'deploy']

export const STAGE_BUCKET_LABELS: Record<StageBucket, string> = {
  ci: 'CI', ready: 'Ready', queue: 'Queue', deploy: 'Deploy', merged: 'Merged',
}

const KIND_TO_BUCKET: Record<StageNode['kind'], StageBucket> = {
  ci: 'ci', gate: 'ready', queue: 'queue', deploy: 'deploy', merged: 'merged',
}

const STAGE_STR_TO_BUCKET: Record<string, StageBucket> = {
  ci: 'ci', ready: 'ready', parked: 'ci', queue: 'queue', merged: 'merged',
}

// A PR's bucket = the kind of its CURRENT track node (active, else last done in
// model order), so a tile's count matches what is lit on the StageTrack.
export function stageBucketOf(pr: PipelinePr, model: StageModel | undefined): StageBucket | null {
  const states = pr.node_states
  if (model && states && states.length) {
    const stateById = new Map(states.map((s) => [s.id, s.state]))
    const nodes = [...model.nodes].sort((a, b) => a.order - b.order)
    let node = nodes.find((n) => stateById.get(n.id) === 'active')
    if (!node) {
      const done = nodes.filter((n) => stateById.get(n.id) === 'done')
      node = done.length ? done[done.length - 1] : undefined
    }
    if (node) return KIND_TO_BUCKET[node.kind] ?? null
  }
  const s = pr.stage
  return (s != null && s !== '' ? STAGE_STR_TO_BUCKET[s] ?? null : null)
}
