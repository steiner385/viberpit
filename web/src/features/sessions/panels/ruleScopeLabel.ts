import type { RuleScope } from '@/api/types'

/** Visible scope names for standing rules (chooser + per-session readout). */
export const RULE_SCOPE_LABEL: Record<RuleScope, string> = {
  session: 'This session',
  repo: 'This repo',
  global: 'All sessions',
}
