// Ported from plotroom@5166b52 frontend/src/spine/laneStatus.ts; edits: ../types → ../types (path unchanged, spine/ is one level below pipeline/)
import type { LaneStatus, LaneView } from '../types';

export const LANE_GLYPH: Record<LaneStatus, string> = {
  green: '●', amber: '◐', red: '✗', blind: '◌', idle: '·',
};
export const LANE_WORD: Record<LaneStatus, string> = {
  green: 'green', amber: 'watch', red: 'red', blind: 'blind', idle: 'idle',
};

export type RollupState = 'red' | 'blind' | 'amber' | 'green';

/** Worst-wins health rollup (spec §4.2). not-wired lanes are excluded; a red
 *  lane whose source is advisory (gating:false) cannot escalate to red. */
const isRed = (l: LaneView) => l.status === 'red' && l.gating;

/** Wired lanes currently needing attention (red+gating, blind, or amber) — the
 *  single source for both the rollup count and the live-region announcement. */
export function attentionLanes(lanes: LaneView[]): LaneView[] {
  return lanes.filter((l) => l.wiredness === 'wired'
    && (isRed(l) || l.status === 'blind' || l.status === 'amber'));
}

/** "N lane needs attention" / "N lanes need attention" with correct subject-verb
 *  agreement, in ONE place so the health band, the spine rollup pill, and the
 *  live-region announcement can't drift (they did: the header rendered "1 lane
 *  need attention" while the spine's live region said "1 lane needs attention"). */
export function attentionPhrase(count: number): string {
  return `${count} ${count === 1 ? 'lane needs' : 'lanes need'} attention`;
}

export function rollup(lanes: LaneView[]): { state: RollupState; count: number; firstAttentionId: string | null } {
  const attention = attentionLanes(lanes);
  const rank = (l: LaneView): number => (isRed(l) ? 3 : l.status === 'blind' ? 2 : l.status === 'amber' ? 1 : 0);
  const worst = attention.reduce<LaneView | null>((a, l) => (a && rank(a) >= rank(l) ? a : l), null);
  const state: RollupState = worst == null ? 'green'
    : isRed(worst) ? 'red' : worst.status === 'blind' ? 'blind' : 'amber';
  const first = attention.find((l) => rank(l) === (worst ? rank(worst) : 0)) ?? null;
  return { state, count: attention.length, firstAttentionId: first?.id ?? null };
}
