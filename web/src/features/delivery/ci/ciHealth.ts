/**
 * One judgement per repo for CI health, in the pipeline's lane vocabulary
 * (`pipeline/spine/laneStatus`: green · watch · red · idle), so a repo reads
 * the same on the Delivery list rail, in its detail band, and on the Pipeline
 * page's health header. The numbers were shown with no verdict before —
 * "1 flaky · slowest e2e 6m 40s" on a neutral rail, 12px cells with no tone —
 * and the reader had to know the thresholds (2026-10-01 Delivery critique, P2).
 *
 * The thresholds are the CI/CD Doctor's own, read from its row (`config` over
 * its `configSchema` defaults), never restated here: the automation that
 * graduates and demotes checks on them is the single source of what "flaky"
 * means. Regressions carry no web threshold either — the server lists a row
 * only past its own ratio (`server/pipeline/history.py`, `_REG_RATIO`).
 */
import type { Automation } from '@/api/automations'
import type { CheckInsights } from '@/api/types'
import type { LaneStatus } from '@/pipeline/types'

export const CICD_DOCTOR_ID = 'cicd-doctor'

export interface CiThresholds {
  /** `max_flake_pct`: the most a check may flake and still graduate. At or
   *  above it a repo is worth watching. */
  maxFlakePct: number
  /** `demote_flake_pct`: the flake rate that demotes a required check. At or
   *  above it a repo is red. */
  demoteFlakePct: number
}

function numberSetting(a: Automation, key: string): number | null {
  const configured = a.config?.[key]
  if (typeof configured === 'number') return configured
  const fallback = a.configSchema.find((f) => f.key === key)?.default
  return typeof fallback === 'number' ? fallback : null
}

/** The CI/CD Doctor's flake thresholds, or null until its row is in hand (the
 *  list is one fetch the whole Delivery page shares). */
export function ciThresholds(automations: readonly Automation[] | undefined): CiThresholds | null {
  const doctor = automations?.find((a) => a.id === CICD_DOCTOR_ID)
  if (!doctor) return null
  const maxFlakePct = numberSetting(doctor, 'max_flake_pct')
  const demoteFlakePct = numberSetting(doctor, 'demote_flake_pct')
  if (maxFlakePct == null || demoteFlakePct == null) return null
  return { maxFlakePct, demoteFlakePct }
}

export interface CiHealth {
  status: LaneStatus
  /** "2 flaky ≥5% · 1 regression" — the reasons, worst first; empty when green. */
  reasons: string[]
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`

/** Worst wins, like the pipeline's rollup: red beats watch beats green. A
 *  repo with no check history in the window is idle — nothing ran, which is
 *  not a verdict on what runs. While the thresholds are unknown, flake rates
 *  are reported but not judged. */
export function ciHealth(data: CheckInsights | undefined, thresholds: CiThresholds | null): CiHealth {
  if (!data || (data.slowest.length === 0 && data.flakiest.length === 0 && data.regressions.length === 0)) {
    return { status: 'idle', reasons: [] }
  }
  const reasons: string[] = []
  let status: LaneStatus = 'green'
  if (thresholds) {
    const red = data.flakiest.filter((f) => f.flake_pct >= thresholds.demoteFlakePct).length
    const watch = data.flakiest.filter((f) => f.flake_pct >= thresholds.maxFlakePct).length - red
    if (red > 0) { status = 'red'; reasons.push(`${red} flaky ≥${thresholds.demoteFlakePct}%`) }
    if (watch > 0) { if (status === 'green') status = 'amber'; reasons.push(`${watch} flaky ≥${thresholds.maxFlakePct}%`) }
  }
  if (data.regressions.length > 0) {
    if (status === 'green') status = 'amber'
    reasons.push(plural(data.regressions.length, 'regression'))
  }
  return { status, reasons }
}

/** The tone a single flake rate reads in beside its number. */
export function flakeStatus(flakePct: number, thresholds: CiThresholds | null): LaneStatus {
  if (!thresholds) return 'idle'
  if (flakePct >= thresholds.demoteFlakePct) return 'red'
  if (flakePct >= thresholds.maxFlakePct) return 'amber'
  return 'green'
}
