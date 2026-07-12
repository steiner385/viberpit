// Ported from plotroom@5166b52 frontend/src/spine/__tests__/laneStatus.test.ts; edits: ../laneStatus → ../spine/laneStatus; ../../types → ../types
import { describe, it, expect } from 'vitest';
import { LANE_GLYPH, LANE_WORD, rollup, attentionPhrase } from '../spine/laneStatus';
import type { LaneView } from '../types';

const lane = (p: Partial<LaneView>): LaneView => ({
  id: 'x', title: 'X', status: 'green', summary: '', wiredness: 'wired', gating: true, ...p,
});

describe('lane status vocabulary', () => {
  it('maps each status to a distinct glyph and word', () => {
    expect(new Set(Object.values(LANE_GLYPH)).size).toBe(5);
    expect(LANE_WORD.blind).toBe('blind');
  });
});

describe('rollup (worst-wins: red > blind > amber > green/idle)', () => {
  it('reports red when any gating lane is red', () => {
    const r = rollup([lane({ status: 'green' }), lane({ id: 'b', status: 'red' })]);
    expect(r.state).toBe('red'); expect(r.count).toBe(1); expect(r.firstAttentionId).toBe('b');
  });
  it('ranks blind above amber', () => {
    expect(rollup([lane({ status: 'amber' }), lane({ id: 'b', status: 'blind' })]).state).toBe('blind');
  });
  it('a red lane with gating:false does NOT escalate to red', () => {
    expect(rollup([lane({ id: 'b', status: 'red', gating: false })]).state).toBe('green');
  });
  it('excludes not-wired lanes from the rollup entirely', () => {
    expect(rollup([lane({ id: 'b', status: 'red', wiredness: 'not-wired' })]).state).toBe('green');
  });
  it('all green/idle → green', () => {
    expect(rollup([lane({ status: 'green' }), lane({ id: 'b', status: 'idle' })]).state).toBe('green');
  });
});

describe('attentionPhrase — subject-verb agreement', () => {
  it('uses the singular verb for one lane', () => {
    expect(attentionPhrase(1)).toBe('1 lane needs attention');
  });
  it('uses the plural verb for zero or many lanes', () => {
    expect(attentionPhrase(0)).toBe('0 lanes need attention');
    expect(attentionPhrase(3)).toBe('3 lanes need attention');
  });
});
