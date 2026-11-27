/**
 * Settings › General › AI assistant: the standing "Always allow" rules that answer the
 * assistant's confirmation cards (#3527), each revocable. A rule is the operator's own — the
 * assistant cannot create, change or revoke one (its token reaches none of the rule routes) —
 * so this list is where one is taken back. Revoking takes effect on the next call. The full
 * editor for every rule stays Settings › Always allow.
 */
import { Link } from 'react-router-dom'
import { usePermissionRules } from '@/api/hooks'
import type { RuleWire } from '@/api/types'
import { mcpToolStem } from '@/brand'
import { relTime, isValidTimestamp } from '@/lib/relativeTime'
import { Button } from '@/ui/Button'
import { appActionTitle } from '@/session/appActions'
import { hitsLabel } from '@/features/permissionRules/ruleScopeLabel'
import { useRevokeRule } from '@/features/permissionRules/useRevokeRule'

/** In Settings there is no "this" conversation, so the lifetimes read as what they cover. */
const LIFETIME: Record<RuleWire['scope'], string> = {
  session: 'One conversation',
  repo: 'One repo',
  global: 'Always',
}

/** A rule on the app's own MCP tools — the only ones the assistant calls. */
const isAssistantRule = (r: RuleWire) => mcpToolStem(r.pattern) !== undefined

function RuleItem({ rule }: { rule: RuleWire }) {
  const { onRevoke, revokeError, dialog } = useRevokeRule(rule.id)
  const stem = mcpToolStem(rule.pattern) ?? rule.pattern
  const last = isValidTimestamp(rule.lastHitAt) ? relTime(rule.lastHitAt) : 'never'
  return (
    <li data-testid={`assistant-rule-${rule.id}`} className="flex items-start justify-between gap-2 py-2 text-sm">
      <div className="min-w-0">
        <div className="text-ink">
          {appActionTitle(rule.pattern) ?? stem} <code className="font-mono text-xs text-ink-muted">{stem}</code>
        </div>
        <div className="text-xs text-ink-muted">
          {LIFETIME[rule.scope]} · {hitsLabel(rule.hitCount)} · last {last}
        </div>
        {revokeError && <p role="alert" className="text-xs text-danger">{revokeError}</p>}
      </div>
      <Button size="sm" variant="secondary" aria-label={`Revoke always-allow for ${stem}`}
        onClick={() => void onRevoke()}>
        Revoke
      </Button>
      {dialog}
    </li>
  )
}

export function AssistantRulesList() {
  const { data: rules = [], isLoading, isError } = usePermissionRules()
  const mine = rules.filter(isAssistantRule)
  return (
    <section aria-labelledby="assistant-rules-heading" className="py-3">
      <h3 id="assistant-rules-heading" className="text-sm font-medium text-ink">Always-allow rules</h3>
      <p className="text-xs text-ink-muted">
        Actions the assistant may take without a confirmation card. Only you can add one — choose{' '}
        <strong>Always allow…</strong> on a card — and deploys, deletes, merges, fan-outs and account
        or product changes always ask. See every rule in{' '}
        <Link to="/settings/always-allow" className="text-accent underline">Always allow</Link>.
      </p>
      {isLoading ? (
        <p className="mt-1 text-xs text-ink-muted">Loading rules…</p>
      ) : isError && rules.length === 0 ? (
        <p className="mt-1 text-xs text-ink-muted">Always-allow rules are unavailable.</p>
      ) : mine.length === 0 ? (
        <p className="mt-1 text-xs text-ink-muted">None — every assistant action asks first.</p>
      ) : (
        <ul className="divide-y divide-edge">{mine.map((r) => <RuleItem key={r.id} rule={r} />)}</ul>
      )}
    </section>
  )
}
