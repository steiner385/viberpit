// Ported from plotroom@5166b52 frontend/src/__tests__/PipelineView.perf.test.tsx; edits: import repointing only (../PipelineView, ../types) — zero assertion edits.
/**
 * Perf test for PipelineView memoization (Task #177).
 * Proves that the expensive repos sort (focused-repo-first) is not recomputed
 * on rerender with the same state + focusedRepo props.
 *
 * Strategy: spy on the `ordering` module's helpers to detect extra derivation.
 * Actually we spy on the `splitCohort` function from ordering.ts — it's called
 * once per visible repo on every render. If repos is re-derived (not memoized),
 * the same work happens; but splitCohort is only in the render path, not the
 * memo computation itself, so that won't work directly.
 *
 * Better strategy: count how many times the sort comparator fires. We do this
 * by injecting a comparator counter via Array.prototype.sort, but scoped to
 * only the repos array by checking array length matches our state.repos.length.
 *
 * Simplest reliable strategy: spy on `nextToMerge` from queueFront.ts (called
 * once per repo in the render path — not in any useMemo). Since repos comes
 * from useMemo, if we rerender with same state + same focusedRepo:
 *   - Without memo: repos is a new array → allPrs is new → StatusStrip recomputes
 *   - With memo: repos is SAME array reference → useMemo([repos]) for allPrs also stable
 *
 * We test this by tracking how many times `splitCohort` is called, which is
 * called in the render body and not inside a memo — so if repos changes
 * identity (no memo), the entire map re-runs, calling splitCohort again for
 * each repo (same count each render). With memoization the repos reference is
 * stable but splitCohort still runs on re-render (it's not memoized itself).
 *
 * Cleaner: use a render-count ref approach. We create a wrapper that increments
 * a counter every time PipelineView renders, then verify the memo reduced recomputation.
 *
 * ACTUAL approach that tests the useMemo directly:
 * - Spy on Array.prototype.flatMap (called by repos.flatMap in allPrs computation)
 * - With useMemo([repos]): flatMap is called only once (on 1st render, not 2nd)
 * - Without useMemo: flatMap is called on every render
 *
 * This tests BOTH the repos useMemo (since allPrs depends on repos) AND the
 * allPrs useMemo (flatMap is cached when repos is stable).
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render } from '@testing-library/react';
import { PipelineView } from '../PipelineView';
import type { DashboardState } from '../types';

function makeState(): DashboardState {
  return {
    generatedAt: '2026-06-17T00:00:00Z',
    staleSince: null,
    repos: [
      {
        repo: 'o/a', hasDeploy: false,
        prs: [],
        queue: null,
        laneHealth: null,
        flake: null,
        deploy: null,
      },
      {
        repo: 'o/b', hasDeploy: false,
        prs: [],
        queue: null,
        laneHealth: null,
        flake: null,
        deploy: null,
      },
    ],
  } as unknown as DashboardState;
}

afterEach(() => { vi.restoreAllMocks(); });

describe('PipelineView memoization perf (Task #177)', () => {
  it('repos derivation (sort) is not recomputed on rerender with same state + focusedRepo', () => {
    // Track flatMap calls specifically on the repos array.
    // The repos array has exactly state.repos.length elements (2).
    // allPrs = repos.flatMap(r => r.prs) → flatMap on a 2-element array.
    // We count flatMap calls on arrays of length 2 as a proxy for allPrs recomputation.
    // With useMemo([state, focusedRepo]) → repos is stable → useMemo([repos]) → flatMap called once.
    // Without useMemo → new array each render → flatMap called each render.
    let flatMapCount = 0;
    const origFlatMap = Array.prototype.flatMap;
    const flatMapSpy = vi.spyOn(Array.prototype, 'flatMap').mockImplementation(function(this: unknown[], ...args) {
      if ((this as unknown[]).length === 2) flatMapCount++;
      return origFlatMap.apply(this, args as Parameters<typeof origFlatMap>);
    });

    const state = makeState();
    const { rerender } = render(<PipelineView state={state} focusedRepo="o/a" />);
    const countAfterFirstRender = flatMapCount;

    rerender(<PipelineView state={state} focusedRepo="o/a" />);
    const countAfterSecondRender = flatMapCount;

    flatMapSpy.mockRestore();

    // Without memoization: flatMap runs again on 2nd render (countAfterSecondRender > countAfterFirstRender).
    // With useMemo: repos is stable, so useMemo([repos]) for allPrs skips the flatMap.
    expect(countAfterSecondRender).toBe(countAfterFirstRender);
  });
});
