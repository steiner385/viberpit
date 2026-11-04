import type { CheckView } from './types';

/** Check conclusions that count as "already failed" for the ready+auto-merge
 *  gate. NEUTRAL/SKIPPED/SUCCESS are not failures; null = still running. */
const FAILING_CONCLUSIONS = new Set(['FAILURE', 'TIMED_OUT', 'STARTUP_FAILURE', 'ACTION_REQUIRED']);

/** What `readyMergeBlock` reads — the structural slice of a `PrView`, so the session
 *  PR tab (D-15) asks the same question of its own PR shape. Conclusions UPPERCASE. */
export interface ReadyMergeInput {
  mergeStateStatus?: string | null;
  checks: readonly Pick<CheckView, 'name' | 'isRequired' | 'conclusion'>[];
}

/**
 * Why arming ready+auto-merge is blocked, or null when it is not: the PR conflicts
 * with base (mergeStateStatus DIRTY), or a required check has ALREADY concluded as a
 * failure. Arming on a still-running PR is safe — GitHub waits for green.
 *
 * The block half of `PrRow`'s `readyMergeGate`, in its own module so the session PR
 * tab's lazy chunk does not pull the Pipeline row and its charts in with it. Which
 * PRs get the button at all is each surface's call (the Pipeline: drafts only).
 */
export function readyMergeBlock(pr: ReadyMergeInput): string | null {
  if (pr.mergeStateStatus === 'DIRTY') return 'conflicts with base — rebase before it can merge';
  const failing = pr.checks.find(
    (c) => c.isRequired && c.conclusion != null && FAILING_CONCLUSIONS.has(c.conclusion));
  return failing ? `required check failing (${failing.name}) — fix before arming` : null;
}
