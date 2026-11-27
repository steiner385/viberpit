// Ported from plotroom@5166b52 frontend/src/sections/pipeline/queueFront.ts; edits: import repointing (./types)
// Front-of-queue derivation (roadmap 4.4) — answers the operator's hourly question
// "what merges next?". Pure: the building train closest to done, else the position-1
// waiting entry with its p50 ETA. The merge-queue/CD depth a release engineer lives in.
import type { RepoQueueView } from './types';

export interface NextToMerge { prNumbers: number[]; percent: number | null; etaSeconds: number | null; building: boolean }

export function nextToMerge(queue: RepoQueueView | null): NextToMerge | null {
  if (!queue) return null;
  const building = queue.groups.filter((g) => !g.failed);
  if (building.length) {
    const front = building.reduce((a, b) => ((b.percent ?? 0) > (a.percent ?? 0) ? b : a));
    return { prNumbers: front.prNumbers, percent: front.percent, etaSeconds: front.etaSeconds, building: true };
  }
  // A null position (queued per the PR record, not yet in the queue snapshot)
  // sorts behind every known position.
  const pos = (p: number | null) => p ?? Number.POSITIVE_INFINITY;
  const w = [...queue.waiting].sort((a, b) => pos(a.position) - pos(b.position))[0];
  if (w) return { prNumbers: [w.prNumber], percent: null, etaSeconds: w.sim?.p50Secs ?? null, building: false };
  return null;
}
