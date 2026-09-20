/**
 * The capacity governor's readout (design 2026-09-19 §5) — one wire shape,
 * `GET /api/capacity`, built server-side by `server/automations/capacity_readout.py:
 * build_readout()` and consumed by both the governor loop and this client. Field
 * names are camelCase exactly as the server emits; no renaming layer.
 */
import { api } from './client'

export interface CapacityHost {
  name: string
  enabled: boolean
  healthy: boolean | null
  active: number
  max: number | null
  free: number | null
}

export interface CapacityAccountWindow {
  key: string
  pct: number | null
  resetsAt: string | null
}

export interface CapacityAccountRow {
  name: string
  engine: string
  provider: string | null
  enabled: boolean
  metered: boolean
  available: boolean
  reason: string | null
  nextResetAt: string | null
  windows: CapacityAccountWindow[]
}

export interface CapacityAccounts {
  available: number
  total: number
  reservePct: number
  nextResetAt: string | null
  rows: CapacityAccountRow[]
  /** Governor config `sessions_per_account` (spec 2026-09-20 §5.2 Runner row). */
  sessionsPerAccount: number
  /** `available × sessionsPerAccount`: the most runner sessions the accounts can carry. */
  accountCap: number
}

export interface CapacityRunner {
  /** The operator's own stored `max_concurrent` — unchanged by any plan overlay,
   *  it is the governor's own planner input. */
  ceiling: number
  /** What the runner actually enforces right now: `min(ceiling, derived)` while
   *  the plan is live, else `ceiling` itself. */
  effective: number
  inFlight: number
  /** The live plan's own `fleetConcurrency`, or `null` when no plan is governing
   *  this tick (not live) — never shown as if a stale/disabled plan still governs. */
  derived: number | null
}

/** One row of the products table — already rank-ordered by the server. */
export interface CapacityProduct {
  slug: string
  name: string | null
  rank: number | null
  createdAt: string | null
  dispatch: string
  capacityMode: 'auto' | 'pinned'
  pin: number
  effectiveCap: number
  inFlight: number
  eligible: number
  constraint: string | null
  waitingOn: string | null
  defect: boolean
}

export type CapacityBindingLayer =
  | 'global' | 'host' | 'account' | 'runner' | 'product' | 'defect' | 'ownership' | 'unknown'

export interface CapacityBinding {
  layer: CapacityBindingLayer
  code: string
  text: string
}

export interface CapacityDefect {
  product: string | null
  target: string
  sessionId: string
  sessionStatus: string | null
  text: string
}

export interface CapacityPlan {
  computedAt: string | null
  pushLevel: string
  applied: boolean
  fleetConcurrency: number
  perProduct: Record<string, number>
  pending: Record<string, number>
  budget: Record<string, unknown>
  binding: CapacityBinding | null
  reasons: string[]
  defects: CapacityDefect[]
}

export type PushLevel = 'conserve' | 'steady' | 'max'

export interface CapacityReadout {
  computedAt: string
  pushLevel: PushLevel
  governorMode: string
  killSwitch: boolean
  /** The weekly reserve each push level keeps (server `RESERVE_PCT`); the radio's descriptions. */
  reservePctByLevel: Record<PushLevel, number>
  global: {
    cap: number; active: number; free: number
    /** The derivation (spec 2026-09-20 D3); null when the server has no seam wired. */
    physical: number | null; attention: number | null
    binding: 'physical' | 'attention' | 'default' | null
  }
  hosts: CapacityHost[]
  accounts: CapacityAccounts
  runner: CapacityRunner
  products: CapacityProduct[]
  binding: CapacityBinding | null
  defects: CapacityDefect[]
  plan: CapacityPlan | null
  /** Whether `plan` is the one actually governing this tick (`server/automations/
   *  capacity_apply.py:plan_is_live` — enabled, applied, computed within the last
   *  10 minutes). `false` while `plan.applied` is true means the runner has fallen
   *  back to each product's stored pin/policy until the governor ticks again. */
  planLive: boolean
}

export function fetchCapacity(): Promise<CapacityReadout> {
  return api.get<CapacityReadout>('/capacity')
}

/** `GET /api/capacity/binding` — the governor's persisted binding constraint, a
 *  one-row read cheap enough for the app-wide nav poll. */
export function fetchCapacityBinding(): Promise<{ binding: CapacityReadout['binding'] }> {
  return api.get<{ binding: CapacityReadout['binding'] }>('/capacity/binding')
}
