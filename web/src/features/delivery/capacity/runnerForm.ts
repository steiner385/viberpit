import type { CapacityReadout } from '@/api/capacity'

/** One namespaced draft for the whole Capacity page's Form save model (spec
 *  2026-09-20 §5.2 item 3): product pins plus the runner's and governor's
 *  Capacity-owned fields, so ONE SaveBar saves all of it. Namespacing keeps a
 *  product slug from ever colliding with a runner key. */
export const PIN_KEY = (slug: string) => `pin:${slug}`
export const PIPELINE_KEY = (slug: string) => `pipeline:${slug}`
export const RUNNER_KEYS = {
  ceiling: 'runner:max_concurrent',
  budget: 'runner:max_spawns_per_day',
  perAccount: 'governor:sessions_per_account',
} as const

export type CapacityDraft = Record<string, string>

function str(v: unknown): string {
  return typeof v === 'number' ? String(v) : ''
}

/** Blank = unlimited (null); otherwise a whole number ≥ 0. The inverse of
 *  `parseWhole`'s `blank: 'null'` mode, used wherever a cap is rendered back
 *  into a draft string (the seed and each row). */
export function capDraft(cap: number | null): string {
  return cap == null ? '' : String(cap)
}

export function seedCapacityDraft(
  readout: CapacityReadout,
  runnerConfig: Record<string, unknown> | null,
  governorConfig: Record<string, unknown> | null,
): CapacityDraft {
  const draft: CapacityDraft = {}
  for (const p of readout.products) {
    draft[PIN_KEY(p.slug)] = String(p.pin)
    draft[PIPELINE_KEY(p.slug)] = capDraft(p.pipelineCap)
  }
  // The readout's `ceiling` IS the stored max_concurrent, so it is the fallback
  // when the automations list has not loaded; budget and per-account have no
  // readout twin and stay blank (the fields render disabled) until it has.
  draft[RUNNER_KEYS.ceiling] = runnerConfig ? str(runnerConfig.max_concurrent) || String(readout.runner.ceiling) : String(readout.runner.ceiling)
  draft[RUNNER_KEYS.budget] = runnerConfig ? str(runnerConfig.max_spawns_per_day) : ''
  draft[RUNNER_KEYS.perAccount] = governorConfig ? str(governorConfig.sessions_per_account) : ''
  return draft
}

export interface RunnerPatches {
  runner: Record<string, number> | null
  governor: Record<string, number> | null
  /** The ceiling went up: raising spends quota, so the page confirms first. */
  ceilingRaised: boolean
}

/** The Capacity page's one "whole number ≥ min" rule (runner ceiling, daily
 *  budget, sessions per account, product pin, pipeline cap). `blank` picks
 *  what an empty field means: `'throw'` for fields with no "unset" value,
 *  `'null'` where blank is itself a legitimate value (unlimited). */
export function parseWhole(raw: string, opts: { label: string; min: number; blank: 'throw' }): number
export function parseWhole(raw: string, opts: { label: string; min: number; blank: 'null' }): number | null
export function parseWhole(raw: string, opts: { label: string; min: number; blank: 'throw' | 'null' }): number | null {
  const trimmed = raw.trim()
  if (trimmed === '' && opts.blank === 'null') return null
  const n = Number(trimmed)
  if (trimmed === '' || !Number.isInteger(n) || n < opts.min) {
    throw new Error(`${opts.label} must be a whole number ≥ ${opts.min}`)
  }
  return n
}

function whole(raw: string, label: string, min: number): number {
  return parseWhole(raw, { label, min, blank: 'throw' })
}

/** Blank = unlimited (null); otherwise a whole number ≥ 0. Throws the row-named
 *  message the form's banner shows. */
export function parsePipelineCap(raw: string, name: string): number | null {
  return parseWhole(raw, { label: `${name}: pipeline cap`, min: 0, blank: 'null' })
}

/** Validates and diffs the runner/governor part of the draft. Throws with the
 *  user-facing message on an invalid field (the form's banner shows it). */
export function parseRunnerDraft(
  draft: CapacityDraft,
  current: { ceiling: number; budget: number | null; perAccount: number | null; globalCap: number },
): RunnerPatches {
  const ceiling = whole(draft[RUNNER_KEYS.ceiling] ?? '', 'Runner ceiling', 0)
  if (ceiling > current.globalCap) {
    throw new Error(`Runner ceiling must not exceed the global cap (${current.globalCap})`)
  }
  const runner: Record<string, number> = {}
  if (ceiling !== current.ceiling) runner.max_concurrent = ceiling
  // A REGISTERED automation whose row never stored the key (rows are created
  // with no config; GET /api/automations serves `row.config or {}` raw) has
  // `current.budget === null` even though the field renders enabled and
  // editable. Gating parsing on `current.budget !== null` alone silently
  // dropped a typed value in that case — check the draft too, not just the
  // stored current value.
  const budgetRaw = draft[RUNNER_KEYS.budget] ?? ''
  if (current.budget !== null || budgetRaw.trim() !== '') {
    const budget = whole(budgetRaw, 'Daily budget', 0)
    if (budget !== current.budget) runner.max_spawns_per_day = budget
  }
  let governor: Record<string, number> | null = null
  const perAccountRaw = draft[RUNNER_KEYS.perAccount] ?? ''
  if (current.perAccount !== null || perAccountRaw.trim() !== '') {
    const perAccount = whole(perAccountRaw, 'Sessions per account', 1)
    if (perAccount !== current.perAccount) governor = { sessions_per_account: perAccount }
  }
  return {
    runner: Object.keys(runner).length > 0 ? runner : null,
    governor,
    ceilingRaised: ceiling > current.ceiling,
  }
}
