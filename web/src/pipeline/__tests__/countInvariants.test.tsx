// Pipeline board count invariants (follow-up to #3549): every PR lands in exactly
// one status tile, and the header's PR CI lane counts with the tiles' own
// predicates. The server half (queue lane == queue stages, deploy stages ==
// deploy backlog) lives in server/tests/test_pipeline_count_invariants.py.
import { cleanup, render, screen } from '@testing-library/react';
import fc from 'fast-check';
import { afterEach, describe, expect, it } from 'vitest';
import { StatusStrip, TILE_DEFINITIONS, bucketPr, isCiRedPr } from '../StatusStrip';
import { prCiLane } from '../spine/lanes/prCiLane';
import type { DashboardState, PrView, StageId } from '../types';

afterEach(cleanup);

const STAGES: StageId[] = ['ci', 'parked', 'ready', 'queue', 'qa-deploy', 'awaiting-prod', 'merged'];
// Every substate the server emits (server/pipeline/dashboard_state.py), plus null
// and an unknown string — a future substate must still land in exactly one tile.
const SUBSTATES = [null, 'draft', 'conflicting', 'ci-failed', 'stuck-enqueue', 'group-failed',
  'unmergeable', 'queue-blocked', 'something-new'];

const arbPr = fc.record({
  number: fc.integer({ min: 1, max: 1_000_000 }),
  stage: fc.constantFrom(...STAGES),
  substate: fc.constantFrom(...SUBSTATES),
}).map(({ number, stage, substate }) => ({
  repo: 'o/r', number, title: `#${number}`, url: '',
  stage: { stage, substate, percent: null, etaSeconds: null, etaRangeSeconds: null, overdue: false },
  queueAheadCount: null, checks: [], groupChecks: null, mergeEtaSim: null,
}) as PrView);

const BUCKETS = new Set(TILE_DEFINITIONS.map((t) => t.bucket));

describe('status tile count invariants', () => {
  it('every PR maps to exactly one tile bucket', () => {
    fc.assert(fc.property(arbPr, (pr) => {
      expect(BUCKETS.has(bucketPr(pr))).toBe(true);
    }));
  });

  it('the six tile numbers sum to the PR count — nothing double counted, nothing dropped', () => {
    fc.assert(fc.property(fc.array(arbPr, { maxLength: 30 }), (prs) => {
      cleanup();
      render(<StatusStrip prs={prs} activeFilter={null} onFilter={() => {}} />);
      const group = screen.getByRole('group', { name: /status overview/i });
      const total = [...group.querySelectorAll('b')]
        .reduce((n, b) => n + Number(b.textContent), 0);
      expect(total).toBe(prs.length);
    }), { numRuns: 40 });
  });

  it("the lane's red cars are Failed; its amber cascade car stays In queue", () => {
    const at = (substate: string | null) => ({ stage: { stage: 'queue', substate } }) as unknown as PrView;
    expect(bucketPr(at('group-failed'))).toBe('failed');
    expect(bucketPr(at('unmergeable'))).toBe('failed');
    expect(bucketPr(at('queue-blocked'))).toBe('queued');
    expect(bucketPr(at(null))).toBe('queued');
  });

  it("the header's CI red is a subset of the Failed tile", () => {
    fc.assert(fc.property(arbPr, (pr) => {
      if (isCiRedPr(pr)) expect(bucketPr(pr)).toBe('failed');
    }));
  });

  it("the header's PR CI lane counts with the tiles' predicates", () => {
    const arbRepos = fc.array(fc.array(arbPr, { maxLength: 15 }), { minLength: 1, maxLength: 3 });
    fc.assert(fc.property(arbRepos, (prsPerRepo) => {
      const repos = prsPerRepo.map((prs, i) => ({ repo: `o/r${i}`, hasDeploy: false, prs, queue: null }));
      const all = prsPerRepo.flat();
      const running = all.filter((p) => bucketPr(p) === 'running').length;
      const red = all.filter(isCiRedPr).length;
      const { summary } = prCiLane(repos as unknown as DashboardState['repos']);
      if (red > 0) expect(summary).toContain(`${running} running · ${red} CI red`);
      else if (running > 0) expect(summary).toContain(`${running} running`);
      // Cross-repo scope is said out loud whenever the tiles (focused repo only)
      // could read a different number.
      const withPrs = prsPerRepo.filter((p) => p.length > 0).length;
      if (withPrs > 1 && (red > 0 || running > 0)) expect(summary).toContain(`${withPrs} repos`);
    }));
  });
});
