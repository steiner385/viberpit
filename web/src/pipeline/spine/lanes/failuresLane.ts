// Ported from plotroom@5166b52 frontend/src/spine/lanes/failuresLane.ts; edits: ../../types → ../../types (path unchanged)
import type { DashboardState, LaneStatus } from '../../types';

// TODO(spine): Spec 5 follow-up — ingest cairnea/ci-failure-reporter issues into
// a failure_clusters table and add a Sentry advisory sub-lane. Both need external
// config/credentials, so they ship as separate follow-ups; this lane is the
// self-contained flake intelligence from the dashboard's own check_durations
// history (same-sha fail-then-pass), no new external API calls.

type FlakeSummary = NonNullable<DashboardState['repos'][number]['flake']>;
type FlakeCheck = FlakeSummary['topChecks'][number];

/**
 * Failures & flake lane derivation (Spec 5): a cross-cutting, advisory view of
 * flaky checks across all repos, derived from the dashboard's own history (the
 * flake engine's same-sha fail-then-pass detection — CANCELLED is never a
 * failing verdict, so spot-kills can't inflate a flake rate). gating:false.
 *
 *  - No flaky check anywhere → idle ('no active flake').
 *  - Any flaky check present → amber, naming the top check + its rounded rate.
 *
 * NEVER red: flaky tests pass on retry, so a red rail here would be pure
 * alarm-fatigue. The lane is amber-at-most by construction.
 */
export function failuresLane(repos: DashboardState['repos']): { status: LaneStatus; summary: string } {
  const summaries = repos.map((r) => r.flake).filter(Boolean) as FlakeSummary[];
  const flakyCount = summaries.reduce((n, s) => n + s.flakyCount, 0);
  const allChecks: FlakeCheck[] = summaries.flatMap((s) => s.topChecks);

  if (flakyCount === 0 || allChecks.length === 0) {
    return { status: 'idle', summary: 'no active flake' };
  }

  const top = allChecks.reduce((a, c) => (c.flakeRatePct > a.flakeRatePct ? c : a));
  return { status: 'amber', summary: `${flakyCount} flaky · ${top.name} ${Math.round(top.flakeRatePct)}%` };
}
