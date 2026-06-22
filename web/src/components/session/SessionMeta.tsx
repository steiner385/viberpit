/**
 * SessionMeta — the second-tier session status strip.
 *
 * Shows, glanceably: current account, current model, remaining context,
 * account quota (session / weekly), and a toggle for the sub-agent list.
 * All values are pre-derived by the caller (SessionView); this component is
 * pure presentation.
 */
import { useState } from 'react'
import { cn } from '@/lib/utils'
import { formatResetTime } from '@/lib/formatReset'
import type { SubAgent, SubAgentStatus } from '@/session/selectors'
import { SubAgentList } from './SubAgentList'

interface Quota {
  sessionPct: number
  weeklyPct: number
  sessionResetsAt: string | null
  weeklyResetsAt: string | null
}

/** Tooltip copy for a quota window: spell out the abbreviation + reset time. */
function quotaTitle(window: string, resetsAt: string | null): string {
  const reset = formatResetTime(resetsAt)
  return reset ? `${window} limit · resets ${reset}` : `${window} limit · no active window`
}

interface Props {
  account: string
  model: string | null
  contextPct: number | null
  quota: Quota | null
  subAgents: SubAgent[]
  /** Live status node (the StatusBar) folded into this strip so the session pane
   *  has a single status line instead of two stacked bars. Optional so the
   *  component stays usable standalone. */
  statusSlot?: React.ReactNode
}

const DASH = '—'

/** Worst (highest) status drives the summary dot colour. */
function summaryDot(subAgents: SubAgent[]): SubAgentStatus {
  if (subAgents.some((a) => a.status === 'failed')) return 'failed'
  if (subAgents.some((a) => a.status === 'running')) return 'running'
  return 'done'
}

const DOT_CLASS: Record<SubAgentStatus, string> = {
  running: 'bg-blue-500 animate-pulse',
  done: 'bg-emerald-500',
  failed: 'bg-red-500',
}

export function SessionMeta({ account, model, contextPct, quota, subAgents, statusSlot }: Props) {
  const [open, setOpen] = useState(false)
  const agentCount = subAgents.length

  return (
    <div className="relative">
      <div
        className={cn(
          'flex items-center gap-x-3 gap-y-1 flex-wrap px-3 py-1',
          'bg-gray-100 dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700',
          'text-xs text-gray-600 dark:text-gray-300',
        )}
      >
        {/* Account */}
        <span className="inline-flex items-center gap-1" aria-label="Current account">
          <span className="text-gray-400 dark:text-gray-500">acct</span>
          <span className="font-medium text-gray-900 dark:text-gray-100">{account}</span>
        </span>

        {/* Model */}
        <span className="inline-flex items-center gap-1" aria-label="Current model">
          <span className="text-gray-400 dark:text-gray-500">model</span>
          <span className="font-medium text-gray-900 dark:text-gray-100 truncate max-w-[14rem]">{model ?? DASH}</span>
        </span>

        {/* Remaining context */}
        <span className="inline-flex items-center gap-1" aria-label="Remaining context">
          <span className="text-gray-400 dark:text-gray-500">ctx</span>
          <span className="font-medium tabular-nums text-gray-900 dark:text-gray-100">
            {contextPct != null ? `${contextPct}%` : DASH}
          </span>
        </span>

        {/* Account quota */}
        {quota && (
          <span className="inline-flex items-center gap-1.5" aria-label="Account quota">
            <span className="text-gray-400 dark:text-gray-500">quota</span>
            <span
              className="tabular-nums text-gray-900 dark:text-gray-100"
              title={quotaTitle('Session', quota.sessionResetsAt)}
              aria-label={`Session usage: ${quota.sessionPct}% — ${quotaTitle('Session', quota.sessionResetsAt)}`}
            >
              sess {quota.sessionPct}%
            </span>
            <span className="text-gray-300 dark:text-gray-600">/</span>
            <span
              className="tabular-nums text-gray-900 dark:text-gray-100"
              title={quotaTitle('Weekly', quota.weeklyResetsAt)}
              aria-label={`Weekly usage: ${quota.weeklyPct}% — ${quotaTitle('Weekly', quota.weeklyResetsAt)}`}
            >
              wk {quota.weeklyPct}%
            </span>
          </span>
        )}

        {/* Right cluster: live status (folded-in StatusBar) + sub-agents toggle */}
        {(statusSlot || agentCount > 0) && (
          <div className="ml-auto flex items-center gap-2">
            {statusSlot}
            {agentCount > 0 && (
              <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                aria-expanded={open}
                aria-label={`${agentCount} sub-agents`}
                className={cn(
                  'inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md min-h-[28px]',
                  'hover:bg-gray-200 dark:hover:bg-gray-700 active:bg-gray-300 dark:active:bg-gray-600',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
                )}
              >
                <span className={cn('h-2 w-2 rounded-full', DOT_CLASS[summaryDot(subAgents)])} aria-hidden="true" />
                <span className="tabular-nums text-gray-900 dark:text-gray-100">{agentCount}</span>
                <span className="text-gray-500 dark:text-gray-400">{agentCount === 1 ? 'agent' : 'agents'}</span>
              </button>
            )}
          </div>
        )}
      </div>

      {/* Sub-agent popover */}
      {open && agentCount > 0 && (
        <div
          className={cn(
            'absolute right-2 top-full z-10 mt-1 w-72 max-h-72 overflow-y-auto',
            'rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 shadow-lg px-2 py-1',
          )}
        >
          <SubAgentList subAgents={subAgents} />
        </div>
      )}
    </div>
  )
}
