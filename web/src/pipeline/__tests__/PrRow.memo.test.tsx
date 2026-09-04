// Ported from plotroom@5166b52 frontend/src/__tests__/PrRow.memo.test.tsx; edits: import repointing (../PrRow, ../types); eslint-disable-next-line for the unused `vi`/`afterEach` imports (unused in original source, kept for port fidelity)
/**
 * PrRow memoization tests (#178)
 *
 * Three layers of proof:
 *
 * 1. COMPARATOR UNIT TESTS — test `areEqual` directly.
 *    Proves the comparator logic: returns true (bail) for equal content,
 *    false (re-render) for any field change.
 *
 * 2. MEMO WIRING TEST — spy on the exported `areEqual` during React renders.
 *    React.memo calls the comparator from the reconciler (outside PrRow.tsx),
 *    so vi.spyOn on the namespace DOES intercept it.  When areEqual returns
 *    true, the render body is skipped.  This test proves the wiring:
 *    memo(PrRowInner, areEqual) is actually the exported PrRow, and equal
 *    content causes areEqual to return true (= body skipped).
 *    Without memo(), areEqual would never be called at all.
 *
 * 3. DOM ANTI-STALE-UI TEST — render PrRow, change content, verify DOM updates.
 *    End-to-end guard: if areEqual wrongly bailed on changed content, "72%"
 *    would remain after changing to 95%.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import * as PrRowMod from '../PrRow';
import type { PrView, CheckView } from '../types';

const { areEqual, PrRow } = PrRowMod;

// ---------------------------------------------------------------------------
// Fixture factory
// ---------------------------------------------------------------------------

const makeCheck = (over: Partial<CheckView> = {}): CheckView => ({
  name: 'fast-checks / ESLint',
  status: 'COMPLETED',
  conclusion: 'SUCCESS',
  isRequired: true,
  workflowName: null,
  elapsedSeconds: 180,
  expectedSeconds: 200,
  url: 'https://x/run1',
  expectedLowSeconds: null,
  expectedHighSeconds: null,
  waitKind: null,
  blockedOn: null,
  waitingSeconds: null,
  expectedRunnerWaitSeconds: null,
  flakeRatePct: null,
  likelyFlake: false,
  ...over,
});

const pr = (over: Partial<PrView> = {}): PrView => ({
  repo: 'acme/widgets',
  number: 8962,
  title: 'fix: calendar overlap',
  url: 'https://x/8962',
  stage: {
    stage: 'ci',
    substate: null,
    percent: 72,
    etaSeconds: 240,
    etaRangeSeconds: null,
    overdue: false,
  },
  queueAheadCount: null,
  checks: [
    makeCheck(),
    makeCheck({
      name: 'lighthouse',
      status: 'IN_PROGRESS',
      conclusion: null,
      isRequired: false,
      elapsedSeconds: 60,
      expectedSeconds: 300,
      url: null,
    }),
  ],
  groupChecks: null,
  mergeEtaSim: null,
  ...over,
});

// ---------------------------------------------------------------------------
// Comparator unit tests
// (load-bearing "skips re-render" guard — tests areEqual directly)
// ---------------------------------------------------------------------------

describe('areEqual comparator (#178)', () => {
  it('returns true (bail out) when all props are content-equal clones', () => {
    // Simulate SSE frame: JSON.parse produces a new object with same content
    const basePr = pr();
    const prev = { pr: basePr, hasDeploy: true, queueCulprit: null };
    const next = {
      pr: JSON.parse(JSON.stringify(basePr)) as PrView,
      hasDeploy: true,
      queueCulprit: null,
    };
    // areEqual returning true means React.memo will skip the re-render
    expect(areEqual(prev, next)).toBe(true);
  });

  it('returns false (re-render) when pr stage percent changes', () => {
    const basePr = pr();
    const changedPr: PrView = JSON.parse(JSON.stringify(basePr));
    (changedPr.stage as { percent: number }).percent = 95;
    const prev = { pr: basePr, hasDeploy: true, queueCulprit: null };
    const next = { pr: changedPr, hasDeploy: true, queueCulprit: null };
    expect(areEqual(prev, next)).toBe(false);
  });

  it('returns false (re-render) when a check conclusion changes', () => {
    const basePr = pr();
    const changedPr: PrView = JSON.parse(JSON.stringify(basePr));
    changedPr.checks[0].conclusion = 'FAILURE';
    const prev = { pr: basePr, hasDeploy: true, queueCulprit: null };
    const next = { pr: changedPr, hasDeploy: true, queueCulprit: null };
    expect(areEqual(prev, next)).toBe(false);
  });

  it('returns false (re-render) when hasDeploy changes', () => {
    const basePr = pr();
    const clonePr: PrView = JSON.parse(JSON.stringify(basePr));
    const prev = { pr: basePr, hasDeploy: true, queueCulprit: null };
    const next = { pr: clonePr, hasDeploy: false, queueCulprit: null };
    expect(areEqual(prev, next)).toBe(false);
  });

  it('returns false (re-render) when queueCulprit changes', () => {
    const basePr = pr();
    const clonePr: PrView = JSON.parse(JSON.stringify(basePr));
    const prev = { pr: basePr, hasDeploy: true, queueCulprit: null };
    const next = { pr: clonePr, hasDeploy: true, queueCulprit: 9001 };
    expect(areEqual(prev, next)).toBe(false);
  });

  // `expandable` was removed as a PrRowProps field in Task 5 (it now comes from
  // shell/InteractionContext, not a prop) — there is no longer a field here for
  // areEqual to compare. The re-render-on-change concern moves with it: a
  // context change always propagates to a memoized consumer regardless of the
  // memo comparator, so it needs no areEqual test. The read-only *behavior* on
  // that change is covered directly in PrRow.test.tsx ('expandable=false
  // (kiosk): clicking the row does not open the check panel', 'is hidden in
  // kiosk (non-expandable) mode', 'is inert (no role/tabindex) in
  // non-expandable kiosk mode').
});

// ---------------------------------------------------------------------------
// React.memo wiring test: structural proof + DOM anti-stale-UI guard
//
// React.memo wraps a component with $$typeof = Symbol.for('react.memo').
// The `compare` property on the memo object holds the comparator.
// Checking these structural properties proves the wiring without spying on
// internal calls (which are captured by value at module initialization and
// cannot be intercepted after the fact by vi.spyOn).
// ---------------------------------------------------------------------------

describe('PrRow React.memo wiring (#178)', () => {
  it('skips re-render when pr content is unchanged (React.memo + areEqual wiring)', () => {
    // Prove PrRow is a React.memo component (structural check)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const memoComponent = PrRow as any;
    expect(memoComponent.$$typeof).toBe(Symbol.for('react.memo'));
    // Prove areEqual is the registered comparator
    expect(memoComponent.compare).toBe(areEqual);
    // Prove the comparator returns true for content-equal clones (= bails out)
    const basePr = pr();
    const clonedPr: PrView = JSON.parse(JSON.stringify(basePr));
    expect(
      areEqual(
        { pr: basePr, hasDeploy: true, queueCulprit: null },
        { pr: clonedPr, hasDeploy: true, queueCulprit: null },
      ),
    ).toBe(true);
  });

  it('re-renders when pr content changes (anti-stale-UI guard)', () => {
    // Start at 72% ci stage
    const basePr = pr();
    const { rerender } = render(
      <PrRow pr={basePr} hasDeploy queueCulprit={null} />,
    );
    expect(screen.getByText('72%')).toBeInTheDocument();

    // Change percent to 95% — content is genuinely different
    const changedPr: PrView = JSON.parse(JSON.stringify(basePr));
    (changedPr.stage as { percent: number }).percent = 95;
    rerender(<PrRow pr={changedPr} hasDeploy queueCulprit={null} />);

    // DOM must update — stale-UI guard:
    // if areEqual wrongly returned true, "72%" would remain and "95%" be absent
    expect(screen.queryByText('72%')).not.toBeInTheDocument();
    expect(screen.getByText('95%')).toBeInTheDocument();
  });
});
