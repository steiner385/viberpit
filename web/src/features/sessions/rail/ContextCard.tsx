/**
 * Rail card: what is filling (or bounding) the session — the context-window
 * meter (same numbers and wording as the prompt row's ring), a Compact now
 * action as auto-compaction nears, the MCP servers inline, the standing
 * Always-allow rules, and the account's quota windows.
 */
import type { McpServerState, UsageWindow } from '@/api/types'
import type { Command } from '@/session/events'
import { cn } from '@/lib/utils'
import { contextSeverity, contextUsedLabel, type ContextSeverity } from '@/lib/contextWindow'
import { quotaWindowAriaLabel, quotaWindowTitle, windowLabel } from '@/lib/usageWindows'
import { Button } from '@/ui/Button'
import { TOUCH_FLOOR } from '@/ui/list/listControlStyles'
import { DeferredMcpInlineList } from '../DeferredMcpStatusReadout'
import { SessionRulesReadout } from '../SessionRulesReadout'
import { RailCard } from './RailCard'

const METER_FILL: Record<ContextSeverity, string> = {
  neutral: 'bg-accent',
  warning: 'bg-warning',
  danger: 'bg-danger',
}

interface Props {
  sessionId: string
  account: string
  /** Remaining context, 0–100 (`rt.contextPct`); null before any usage is known. */
  contextPct: number | null
  /** The account's quota windows (`rt.quota`). */
  quota: UsageWindow[] | null
  /** The engine's command catalog (`rt.commands`): Compact now needs `compact`. */
  commands: Command[]
  /** Live MCP status (`rt.state.mcpServers`); null falls back to the GET snapshot. */
  mcpLive: McpServerState[] | null
  /** Send `/compact` as a plain prompt. The card never touches the runtime itself. */
  onCompact: () => void
}

export function ContextCard({ sessionId, account, contextPct, quota, commands, mcpLive, onCompact }: Props) {
  const severity = contextSeverity(contextPct)
  const canCompact = commands.some((c) => c.name === 'compact')

  return (
    <RailCard title="Context">
      {contextPct != null && (
        <div className="flex flex-col gap-1">
          <p className="text-ink">{contextUsedLabel(contextPct)}</p>
          <div
            role="meter"
            aria-label={contextUsedLabel(contextPct)}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={100 - contextPct}
            className="h-1.5 w-full overflow-hidden rounded-pill bg-edge"
          >
            <div className={cn('h-full rounded-pill', METER_FILL[severity])} style={{ width: `${100 - contextPct}%` }} />
          </div>
          {severity === 'danger' && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium text-danger">Auto-compacts soon</span>
              {canCompact && (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={onCompact}
                  className={TOUCH_FLOOR}
                >
                  Compact now
                </Button>
              )}
            </div>
          )}
        </div>
      )}

      <DeferredMcpInlineList sessionId={sessionId} account={account} live={mcpLive} />

      <SessionRulesReadout sessionId={sessionId} />

      {quota && quota.length > 0 && (
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="text-ink-muted">Quota</span>
          <ul aria-label="Account quota" className="flex flex-wrap gap-x-3 gap-y-1">
            {quota.map((w) => (
              <li
                key={w.key}
                className="tabular-nums text-ink"
                title={quotaWindowTitle(w)}
                aria-label={quotaWindowAriaLabel(w)}
              >
                {windowLabel(w)} {w.pct}%
              </li>
            ))}
          </ul>
        </div>
      )}
    </RailCard>
  )
}
