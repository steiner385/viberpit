/**
 * SessionMeta — the second-tier session status strip.
 *
 * Shows, glanceably: the agent (engine) when it is not the default one, current
 * account, current model, reasoning effort, runner host, remaining context,
 * account quota (one readout per provider window), and a toggle for the
 * sub-agent list.
 * All values are pre-derived by the caller (SessionView); this component is
 * pure presentation — including the capability flags, which the caller reads
 * from the server's per-engine capability report rather than inferring here.
 */
import { Fragment, useState } from 'react'
import { cn } from '@/lib/utils'
import { formatResetTime } from '@/lib/formatReset'
import { contextSeverity } from '@/lib/contextWindow'
import type { SubAgent, SubAgentStatus } from '@/session/selectors'
import { engineBadge, modelName } from '@/session/modelName'
import { SubAgentList } from './SubAgentList'
import type { AgentEngine, UsageWindow } from '@/api/types'

/** Compact labels for the strip, which is tight on horizontal space. Falls back
 *  to the provider's own window key when it is not one we abbreviate. */
const QUOTA_ABBR: Record<string, string> = {
  session: 'sess',
  weekly: 'wk',
  premium: 'prem',
}

/** Tooltip copy for a quota window: spell out the abbreviation + reset time. */
function quotaTitle(window: string, resetsAt: string | null): string {
  const reset = formatResetTime(resetsAt)
  return reset ? `${window} limit · resets ${reset}` : `${window} limit · no active window`
}

interface Props {
  account: string
  /** Engine the session runs on. Rendered only when it is not the default one. */
  engine?: AgentEngine | null
  /** Fleet host executing the session; null/absent = no host registry entry. */
  host?: string | null
  /** Where the session's GUI renders; 'native' shows the desktop chip. */
  displayMode?: 'native' | 'virtual' | null
  /** Engine-native reasoning choice (Codex); null = the model's own default. */
  reasoningEffort?: string | null
  /** Server-advertised `subagents` capability for this session's engine. False
   *  hides the sub-agent surface entirely — an engine that never spawns one must
   *  not offer a control for it. Defaults to true so an unanswered capability
   *  report never removes a control that works today. */
  subAgentsSupported?: boolean
  /** The CHOSEN model alias (session record); null = account default. Drives the picker. */
  model: string | null
  /** The model actually producing (SDK id, e.g. "claude-opus-4-8"); shown as a read-only
   *  readout beside the picker. After a switch it lags `model` by one turn. */
  liveModel?: string | null
  /** True while a "switch after this turn" change waits for the in-flight turn to end. */
  modelPending?: boolean
  /** Permission mode for the session ('plan' shows the plan-mode toggle as on). */
  mode: string
  contextPct: number | null
  quota: UsageWindow[] | null
  subAgents: SubAgent[]
  /** Live status node (the StatusBar) folded into this strip so the session pane
   *  has a single status line instead of two stacked bars. Optional so the
   *  component stays usable standalone. */
  statusSlot?: React.ReactNode
  /** Optional export control (e.g. ExportMenu) rendered in the right cluster.
   *  SessionMeta stays free of @/api imports — the caller provides the node. */
  exportSlot?: React.ReactNode
  /** Optional session actions (archive / ⋮ menu) rendered at the end of the right
   *  cluster. Mobile (SessionView) passes its SessionActions here so they share
   *  this strip's status line instead of needing a row of their own; desktop omits
   *  it and keeps the actions in its own header. */
  actionsSlot?: React.ReactNode
  models: { id: string; label: string }[]
  accounts: { name: string; nickname?: string | null }[]
  accountPinned: boolean
  onSelectModel: (id: string) => void
  onSelectAccount: (name: string) => void
  onSetMode: (mode: string) => void
  /** Clear the account pin; the server rotates a `limited` session immediately. */
  onUnpin?: () => void
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

export function SessionMeta({ account, engine = null, host = null, displayMode = null, reasoningEffort = null, subAgentsSupported = true, model, liveModel = null, modelPending = false, mode, contextPct, quota, subAgents, statusSlot, exportSlot, actionsSlot, models, accounts, accountPinned, onSelectModel, onSelectAccount, onSetMode, onUnpin }: Props) {
  const [open, setOpen] = useState(false)
  const agentCount = subAgentsSupported ? subAgents.length : 0
  const agentBadge = engineBadge(engine)

  return (
    <div className="relative">
      <div
        className={cn(
          'flex items-center gap-x-3 gap-y-1 flex-wrap px-3 py-1',
          'bg-gray-100 dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700',
          'text-xs text-gray-600 dark:text-gray-300',
        )}
      >
        {/* Agent (engine). Shown only when it is not the default Claude Code
            engine — `engineBadge` owns that rule for every identity surface. */}
        {agentBadge && (
          <span className="inline-flex items-center gap-1" aria-label="Agent">
            <span className="text-gray-400 dark:text-gray-500">agent</span>
            <span className="font-medium text-gray-900 dark:text-gray-100 truncate max-w-[10rem]">
              {agentBadge}
            </span>
          </span>
        )}

        {/* Account switcher */}
        <span className="inline-flex items-center gap-1">
          <span className="text-gray-400 dark:text-gray-500">acct</span>
          <select
            aria-label="Switch account"
            value={account}
            onChange={(e) => e.target.value && e.target.value !== account && onSelectAccount(e.target.value)}
            className="bg-transparent font-medium text-gray-900 dark:text-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 rounded"
          >
            {accounts.every((a) => a.name !== account) && <option value={account}>{account}</option>}
            {accounts.map((a) => (
              <option key={a.name} value={a.name}>{a.nickname || a.name}</option>
            ))}
          </select>
          {accountPinned && (
            <button
              type="button"
              onClick={onUnpin}
              aria-label="Unpin account"
              title="Pinned — auto-rotation off. Click to unpin."
              className="text-amber-500 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
            >
              📌
            </button>
          )}
        </span>

        {/* Model switcher: bound to the CHOSEN alias (the session record), never to
            the SDK-reported id — that id is the launch default until a turn runs on
            the new model, so binding to it made the picker snap back on every switch. */}
        {(() => {
          const chosen = model ?? 'default'
          const live = liveModel ? modelName(liveModel, engine) : ''
          return (
            <span className="inline-flex items-center gap-1">
              <span className="text-gray-400 dark:text-gray-500">model</span>
              <select
                aria-label="Select model"
                value={chosen}
                onChange={(e) => e.target.value && e.target.value !== chosen && onSelectModel(e.target.value)}
                className="bg-transparent font-medium text-gray-900 dark:text-gray-100 truncate max-w-[14rem] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 rounded"
              >
                {!models.some((m) => m.id === chosen) && (
                  <option value={chosen}>{chosen === 'default' ? 'Default' : chosen}</option>
                )}
                {models.map((m) => (
                  <option key={m.id} value={m.id}>{m.label}</option>
                ))}
              </select>
              {live && (
                <span
                  aria-label="Model in use"
                  title="Model currently answering (a switch takes effect on the next turn)"
                  className="text-gray-400 dark:text-gray-500 truncate max-w-[8rem]"
                >
                  {live}
                </span>
              )}
              {modelPending && (
                <span
                  aria-label="Model switch pending"
                  title="Switching when the current turn finishes"
                  className="text-amber-600 dark:text-amber-400"
                >
                  · after this turn
                </span>
              )}
            </span>
          )
        })()}

        {/* Reasoning effort — the engine-native depth choice (Codex). Absent
            means "the model's own default", which is not worth a readout. It is
            data-gated, not capability-gated: an engine without the capability
            never reports one. */}
        {reasoningEffort && (
          <span className="inline-flex items-center gap-1" aria-label="Reasoning effort">
            <span className="text-gray-400 dark:text-gray-500">effort</span>
            <span className="font-medium text-gray-900 dark:text-gray-100 truncate max-w-[8rem]">
              {reasoningEffort}
            </span>
          </span>
        )}

        {/* Runner host — which fleet member is actually executing this session. */}
        {host && (
          <span className="inline-flex items-center gap-1" aria-label="Runner host">
            <span className="text-gray-400 dark:text-gray-500">host</span>
            <span className="font-medium text-gray-900 dark:text-gray-100 truncate max-w-[10rem]">
              {host}
            </span>
          </span>
        )}

        {displayMode === 'native' && (
          <span className="inline-flex items-center gap-1" aria-label="Display" title="Windows open on the host's desktop">
            <span className="text-gray-400 dark:text-gray-500">display</span>
            <span className="font-medium text-gray-900 dark:text-gray-100">desktop</span>
          </span>
        )}

        {/* Remaining context */}
        {(() => {
          const sev = contextSeverity(contextPct)
          const color = sev === 'danger'
            ? 'text-red-700 dark:text-red-400'
            : sev === 'warning'
              ? 'text-amber-700 dark:text-amber-400'
              : 'text-gray-900 dark:text-gray-100'
          return (
            <span className={cn('inline-flex items-center gap-1', color)} aria-label="Remaining context">
              <span className="text-gray-400 dark:text-gray-500">ctx</span>
              <span className="font-medium tabular-nums">
                {contextPct != null ? `${contextPct}%` : DASH}
              </span>
              {sev === 'danger' && <span className="opacity-70">· compacts soon</span>}
            </span>
          )
        })()}

        {/* Account quota — one readout per window the provider reported, so a
            single-allowance provider shows ONE number instead of the same one
            twice under two labels. */}
        {quota && quota.length > 0 && (
          <span className="inline-flex items-center gap-1.5" aria-label="Account quota">
            <span className="text-gray-400 dark:text-gray-500">quota</span>
            {quota.map((w, i) => (
              <Fragment key={w.key}>
                {i > 0 && <span className="text-gray-300 dark:text-gray-600">/</span>}
                <span
                  className="tabular-nums text-gray-900 dark:text-gray-100"
                  title={quotaTitle(w.label, w.resetsAt)}
                  aria-label={`${w.label} usage: ${w.pct}% — ${quotaTitle(w.label, w.resetsAt)}`}
                >
                  {QUOTA_ABBR[w.key] ?? w.key} {w.pct}%
                </span>
              </Fragment>
            ))}
          </span>
        )}

        {/* Plan mode toggle — sits with the other session settings */}
        <button
          type="button"
          role="switch"
          aria-checked={mode === 'plan'}
          aria-label="Plan mode"
          onClick={() => onSetMode(mode === 'plan' ? 'default' : 'plan')}
          className={cn(
            'inline-flex items-center rounded px-2 py-0.5 text-xs font-medium min-h-[28px] border',
            mode === 'plan'
              ? 'bg-blue-100 dark:bg-blue-900/40 border-blue-400 text-blue-700 dark:text-blue-300'
              : 'bg-transparent border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-400',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          )}
        >
          plan
        </button>

        {/* Right cluster: live status (folded-in StatusBar) + export + session
            actions (archive/⋮) + sub-agents toggle */}
        {(statusSlot || exportSlot || actionsSlot || agentCount > 0) && (
          <div className="ml-auto flex items-center gap-2">
            {statusSlot}
            {exportSlot}
            {actionsSlot}
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
