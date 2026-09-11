// Ported from plotroom@5166b52 frontend/src/spine/lanes/mainLane.ts; edits: ../../types → ../../types (path unchanged);
// summary names the worst repo's tip + CI verdict (server laneHealth.tipSha/tipCi) in the deploy banner's words
import type { DashboardState, LaneStatus } from '../../types';
import { ciLabel } from '@/lib/ciVerdict';

type LaneHealth = NonNullable<DashboardState['repos'][number]['laneHealth']>;

export function mainLane(repos: DashboardState['repos']): { status: LaneStatus; summary: string } {
  const health = repos.map((r) => r.laneHealth).filter(Boolean) as LaneHealth[];
  const states = health.map((h) => h.main);
  if (states.length === 0) return { status: 'blind', summary: 'no signal' };
  const worst: LaneStatus = states.includes('red') ? 'red'
    : states.includes('blind') ? 'blind' : states.includes('amber') ? 'amber'
    : states.includes('green') ? 'green' : 'idle';
  const word = { green: 'green', amber: 'watch', red: 'red', blind: 'no signal', idle: 'idle' }[worst];
  // Anything but green says which tip and what CI thinks of it, through the same
  // label map the deploy banner uses — "main · watch · 5c8056c CI pending" and the
  // banner's "CI pending" are then one fact in two places, not two opinions.
  const culprit = worst === 'green' ? undefined : health.find((h) => h.main === worst && h.tipSha && h.tipCi);
  const summary = culprit ? `${word} · ${culprit.tipSha!.slice(0, 7)} CI ${ciLabel(culprit.tipCi!)}` : word;
  return { status: worst, summary };
}
