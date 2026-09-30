// Ported from plotroom@5166b52 frontend/src/sections/pipeline/ordering.ts; edits: import repointing (./types, ./StatusStrip)
// Pipeline ordering (roadmap 1.3) — the PR pipeline must lead with what needs eyes,
// not bury 3 running/overdue PRs under 48 identical "awaiting prod" rows. Pure +
// testable. `attentionSort` ranks failed > running > queued > ready > merged > deploy (with
// overdue bumping a PR up within its tier); `splitCohort` peels the non-overdue
// awaiting-prod herd into a collapsible cohort so the lead stays scannable.
import type { PrView } from './types';
import { bucketPr, type Bucket } from './StatusStrip';

const RANK: Record<Bucket, number> = { failed: 0, running: 1, queued: 2, ready: 3, merged: 4, deploy: 5 };

/** Lower = needs more attention. Overdue bumps up by half a tier. */
export function attentionRank(pr: PrView): number {
  const base = RANK[bucketPr(pr)];
  return pr.stage.overdue ? base - 0.5 : base;
}

export function attentionSort(prs: PrView[]): PrView[] {
  return [...prs].sort((a, b) => attentionRank(a) - attentionRank(b) || a.number - b.number);
}

/** The collapsible "awaiting prod" set: merged PRs awaiting the prod deploy that are
 *  NOT overdue (overdue ones stay in the lead — they need attention). */
function inCohort(pr: PrView): boolean {
  return bucketPr(pr) === 'deploy' && !pr.stage.overdue;
}

/** The merged LEDGER: bare `merged` rows — landed, nothing left to do, kept on
 *  the board for the 7-day retention window (`server/pipeline/stage.py`). Live
 *  they were 147 of 170 rows, 132px each, in the lead ahead of nothing but
 *  the deploy cohort (2026-09-30 Pipeline critique, P1). Draft / conflicting
 *  rows share the strip's "Merged / other" bucket but are NOT ledger: a
 *  conflicting PR needs a rebase, so it stays in the lead. An overdue merged
 *  row stays too, for the same reason the overdue cohort row does. */
function inLedger(pr: PrView): boolean {
  return pr.stage.stage === 'merged' && !pr.stage.overdue;
}

export function splitCohort(prs: PrView[]): { lead: PrView[]; cohort: PrView[]; merged: PrView[] } {
  return {
    lead: attentionSort(prs.filter((p) => !inCohort(p) && !inLedger(p))),
    cohort: prs.filter(inCohort),
    merged: prs.filter(inLedger),
  };
}

/** The newest `mergedAt` among *prs*, for the ledger line's "last …"; null when
 *  no row carries a timeline (a pre-upgrade payload). */
export function newestMergedAt(prs: readonly PrView[]): string | null {
  let newest: string | null = null;
  for (const p of prs) {
    const at = p.timeline?.mergedAt;
    if (at && (newest === null || at > newest)) newest = at;
  }
  return newest;
}

/** Split a deploy cohort into its disjoint stages so the two aren't lumped under
 *  one "awaiting prod" label: a PR still rolling out to QA (`qa-deploy`) is
 *  awaiting QA; one live on QA but not prod (`awaiting-prod`) is awaiting prod. */
export function deployBreakdown(prs: PrView[]): { awaitingQa: number; awaitingProd: number } {
  let awaitingQa = 0;
  let awaitingProd = 0;
  for (const p of prs) {
    if (p.stage.stage === 'qa-deploy') awaitingQa += 1;
    else if (p.stage.stage === 'awaiting-prod') awaitingProd += 1;
  }
  return { awaitingQa, awaitingProd };
}
