import type { CapacityReadout } from '@/api/capacity'

export const CAPACITY: CapacityReadout = {
  computedAt: '2026-09-19T10:00:00Z',
  pushLevel: 'steady',
  governorMode: 'monitor',
  killSwitch: true,
  global: { cap: 20, active: 10, free: 10, physical: null, attention: null, binding: null },
  hosts: [
    { name: 'dobby', enabled: true, healthy: true, active: 5, max: 10, free: 5 },
  ],
  accounts: {
    available: 2, total: 3, reservePct: 20, nextResetAt: null,
    rows: [
      { name: 'primary', engine: 'claude_code', provider: 'anthropic', enabled: true, metered: true, available: true, reason: null, nextResetAt: null, windows: [] },
    ],
  },
  runner: { ceiling: 5, effective: 5, inFlight: 3, derived: null },
  products: [
    {
      slug: 'kindash', name: 'KinDash', rank: 1, createdAt: '2026-01-01T00:00:00Z',
      dispatch: 'on', capacityMode: 'pinned', pin: 2, effectiveCap: 2, inFlight: 1, eligible: 3,
      constraint: null, waitingOn: null, defect: false,
    },
    {
      slug: 'viberpit', name: 'ViberPit', rank: 2, createdAt: '2026-01-02T00:00:00Z',
      dispatch: 'on', capacityMode: 'auto', pin: 1, effectiveCap: 3, inFlight: 3, eligible: 5,
      constraint: 'product_capacity_full', waitingOn: 'at the product cap (rank 2 of 2)', defect: true,
    },
  ],
  binding: { layer: 'product', code: 'product_capacity_full', text: 'viberpit: at the fleet-wide product cap' },
  defects: [
    {
      product: 'viberpit', target: 'issue:o/r#1', sessionId: 'sess-1', sessionStatus: 'complete',
      text: 'viberpit: claim held by complete session sess-1 — a defect, not load',
    },
  ],
  plan: {
    computedAt: '2026-09-19T09:59:00Z',
    pushLevel: 'steady',
    applied: false,
    fleetConcurrency: 4,
    perProduct: { kindash: 2, viberpit: 2 },
    pending: {},
    budget: {},
    binding: { layer: 'product', code: 'product_capacity_full', text: 'viberpit: at the fleet-wide product cap' },
    reasons: ['kindash: pinned at 2', 'viberpit: capped by a held defect, not real load'],
    defects: [],
  },
  planLive: false,
}
