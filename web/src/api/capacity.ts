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
}

export interface CapacityRunner {
  ceiling: number
  inFlight: number
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
  global: { cap: number; active: number; free: number }
  hosts: CapacityHost[]
  accounts: CapacityAccounts
  runner: CapacityRunner
  products: CapacityProduct[]
  binding: CapacityBinding | null
  defects: CapacityDefect[]
  plan: CapacityPlan | null
}

export function fetchCapacity(): Promise<CapacityReadout> {
  return api.get<CapacityReadout>('/capacity')
}
