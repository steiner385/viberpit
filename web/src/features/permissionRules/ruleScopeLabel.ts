import type { RuleScope } from '@/api/types'

/** Visible scope names and counts for standing rules (chooser, per-session readout, Settings). */
export const RULE_SCOPE_LABEL: Record<RuleScope, string> = {
  session: 'This session',
  repo: 'This repo',
  global: 'All sessions',
}

export const hitsLabel = (n: number) => `${n} ${n === 1 ? 'hit' : 'hits'}`
