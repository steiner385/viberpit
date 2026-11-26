import type { RuleScope } from '@/api/types'

/** Visible scope names and counts for standing rules (chooser, per-session readout, Settings). */
export const RULE_SCOPE_LABEL: Record<RuleScope, string> = {
  session: 'This session',
  repo: 'This repo',
  global: 'All sessions',
}

/** The assistant drawer's names for the two lifetimes it offers (#3527): a session-scope rule
 *  lasts for this conversation only (the safer default); a global rule is "Always". The drawer
 *  has no repo, so it never offers that scope. */
export const ASSISTANT_RULE_SCOPE_LABEL: Partial<Record<RuleScope, string>> = {
  session: 'This conversation only',
  global: 'Always',
}

export const hitsLabel = (n: number) => `${n} ${n === 1 ? 'hit' : 'hits'}`
