/**
 * SessionRulesReadout — the standing "Always allow" rules that apply to this
 * session (the server resolves session + repo + global), with Revoke. Renders
 * nothing when none apply. Management lives in Settings → Always-allow rules.
 */
import { X } from 'lucide-react'
import { usePermissionRules } from '@/api/hooks'
import type { RuleWire } from '@/api/types'
import { useRevokeRule } from '@/features/permissionRules/useRevokeRule'
import { RULE_SCOPE_LABEL, hitsLabel } from '@/features/permissionRules/ruleScopeLabel'

function RuleChip({ rule }: { rule: RuleWire }) {
  const { onRevoke, revokeError, dialog } = useRevokeRule(rule.id)
  return (
    <li data-testid={`session-rule-${rule.id}`} className="inline-flex items-center gap-1.5">
      <code className="font-mono break-all">{rule.pattern}</code>
      <span className="rounded bg-surface-2 px-1 text-ink-muted">{RULE_SCOPE_LABEL[rule.scope]}</span>
      <span className="text-ink-muted tabular-nums">{hitsLabel(rule.hitCount)}</span>
      <button
        type="button"
        aria-label={`Revoke rule ${rule.pattern}`}
        className="inline-flex min-h-target min-w-target items-center justify-center rounded hover:bg-surface-2"
        onClick={() => void onRevoke()}
      >
        <X className="h-3 w-3" aria-hidden />
      </button>
      {revokeError && <span role="alert" className="text-danger">{revokeError}</span>}
      {dialog}
    </li>
  )
}

export function SessionRulesReadout({ sessionId }: { sessionId: string }) {
  const { data: rules = [] } = usePermissionRules(sessionId)
  if (rules.length === 0) return null
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5" aria-label="Always-allow rules">
      <span className="text-ink-muted">rules</span>
      <ul className="inline-flex flex-wrap gap-2">
        {rules.map((r) => <RuleChip key={r.id} rule={r} />)}
      </ul>
    </span>
  )
}
