// Ported from plotroom@5166b52 frontend/src/spine/lanes/prCiLane.ts; edits: counts read StatusStrip's
// bucketPr/isCiRedPr (the tiles' own predicates) and the summary names its cross-repo scope
import type { DashboardState, LaneStatus } from '../../types';
import { bucketPr, isCiRedPr } from '../../StatusStrip';

/** The header's PR CI lane. It counts EVERY repo on the board, while the status
 *  tiles count only the focused repo — so with more than one repo the summary
 *  says so, and "N red" (head CI red) is a subset of the Failed tile, which
 *  also holds queue failures and armed-but-not-enqueued PRs. */
export function prCiLane(repos: DashboardState['repos']): { status: LaneStatus; summary: string } {
  const prs = repos.flatMap((r) => r.prs);
  const running = prs.filter((p) => bucketPr(p) === 'running').length;
  const failed = prs.filter(isCiRedPr).length;
  const repoCount = repos.filter((r) => r.prs.length > 0).length;
  const scope = repoCount > 1 ? ` · ${repoCount} repos` : '';
  if (failed > 0) return { status: 'red', summary: `${running} running · ${failed} CI red${scope}` };
  if (running === 0) return { status: 'idle', summary: 'idle · no PRs in CI' };
  return { status: 'green', summary: `${running} running${scope}` };
}
