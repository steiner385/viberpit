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
import { Fragment, useId, useState } from 'react'
import { useNow } from '@/hooks/useNow'
import { Pin, SlidersHorizontal } from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatResetTime } from '@/lib/formatReset'
import { contextSeverity } from '@/lib/contextWindow'
import { EMPTY_BACKGROUND_WORK_VIEW, type BackgroundWorkView, type SubAgent, type SubAgentStatus } from '@/session/selectors'
import { engineBadge, modelName } from '@/session/modelName'
import { windowLabel } from '@/lib/usageWindows'
import { SubAgentList } from './SubAgentList'
import { BackgroundWorkList } from './BackgroundWorkList'
import { backgroundWorkSummary } from './backgroundWorkSummary'
import { STATUS_TONE, statusTone } from './subAgentStatus'
import type { AgentEngine, UsageWindow } from '@/api/types'

/** Tooltip copy for a quota window: spell out the abbreviation + reset time. */
function quotaTitle(window: string, resetsAt: string | null): string {
  const reset = formatResetTime(resetsAt)
  return reset ? `${window} limit · resets ${reset}` : `${window} limit · no active window`
}

interface Props {
  /** The session this strip belongs to; the sub-agent popover's rows link into
   *  `subAgentPath(sessionId, …)`. */
  sessionId: string
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
  /** Latest requested mode, while the effective mode above is awaiting change. */
  pendingMode?: string | null
  contextPct: number | null
  quota: UsageWindow[] | null
  subAgents: SubAgent[]
  /** Open background work (shells / Monitors / wakeup) for the "Background" chip (#2635). */
  backgroundWork?: BackgroundWorkView
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
  /** In-session MCP readout (rendered right after ctx). */
  mcpSlot?: React.ReactNode
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
  if (subAgents.some((a) => a.status === 'stopped')) return 'stopped'
  return 'done'
}

export function SessionMeta({ sessionId, account, engine = null, host = null, displayMode = null, reasoningEffort = null, subAgentsSupported = true, model, liveModel = null, modelPending = false, mode, pendingMode = null, contextPct, quota, subAgents, backgroundWork = EMPTY_BACKGROUND_WORK_VIEW, statusSlot, exportSlot, actionsSlot, mcpSlot, models, accounts, accountPinned, onSelectModel, onSelectAccount, onSetMode, onUnpin }: Props) {
  const [panel, setPanel] = useState<'agents' | 'background' | null>(null)
  const now = useNow(1000, backgroundWork.wakeupAt != null)
  // `now` is 0 until the first tick; a countdown against 0 would flash a huge number.
  const bgSummary = backgroundWorkSummary(now === 0 ? { ...backgroundWork, wakeupAt: null } : backgroundWork, now)
  const agentCount = subAgentsSupported ? subAgents.length : 0
  const agentBadge = engineBadge(engine)
  // Narrow-strip fold (2026-09-29 re-critique, P2). In a pane under 28rem this
  // strip wrapped three rows deep — 114px of a phone's 844 before the transcript,
  // on top of the TopBar and the header. The live status, export, the session
  // actions and the sub-agent toggle stay on the one visible row; the identity
  // and settings readouts (account, model, effort, host, display, ctx, quota,
  // plan) fold behind a "Details" toggle. Same mechanism as the list controls'
  // fold: this strip is a `@container`, `@max-md:` folds, `display: contents`
  // everywhere else so a wide pane's single row is untouched. Hidden, not
  // unmounted: the selects keep their state and jsdom tests see every control.
  const [detailsOpen, setDetailsOpen] = useState(false)
  const detailsId = useId()

  return (
    <div className="relative">
      <div
        className={cn(
          '@container flex items-center gap-x-3 gap-y-1 flex-wrap px-3 py-1',
          'bg-surface-raised border-b border-edge',
          'text-xs text-ink-muted',
        )}
      >
        <button
          type="button"
          aria-expanded={detailsOpen}
          aria-controls={detailsId}
          // "Session details": the transcript's tool cards have their own
          // "Details" toggles, so a bare name would be one of several.
          aria-label="Session details"
          onClick={() => setDetailsOpen((o) => !o)}
          className={cn(
            'hidden @max-md:inline-flex items-center gap-1 rounded px-2 py-0.5 min-h-7',
            'border border-edge-strong font-medium text-ink-muted',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
          )}
        >
          <SlidersHorizontal size={12} aria-hidden="true" />
          Details
        </button>
        <div
          id={detailsId}
          data-testid="session-meta-details"
          className={cn(
            'contents',
            detailsOpen
              ? '@max-md:flex @max-md:w-full @max-md:flex-wrap @max-md:items-center @max-md:gap-x-3 @max-md:gap-y-1'
              : '@max-md:hidden',
          )}
        >
        {/* Agent (engine). Shown only when it is not the default Claude Code
            engine — `engineBadge` owns that rule for every identity surface. */}
        {agentBadge && (
          <span className="inline-flex items-center gap-1" aria-label="Agent">
            <span className="text-ink-muted">agent</span>
            <span className="font-medium text-ink truncate max-w-[10rem]">
              {agentBadge}
            </span>
          </span>
        )}

        {/* Account switcher */}
        <span className="inline-flex items-center gap-1">
          <span className="text-ink-muted">acct</span>
          <select
            aria-label="Switch account"
            value={account}
            onChange={(e) => e.target.value && e.target.value !== account && onSelectAccount(e.target.value)}
            className="bg-transparent font-medium text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
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
              className="text-warning rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              <Pin size={14} aria-hidden="true" />
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
              <span className="text-ink-muted">model</span>
              <select
                aria-label="Select model"
                value={chosen}
                onChange={(e) => e.target.value && e.target.value !== chosen && onSelectModel(e.target.value)}
                className="bg-transparent font-medium text-ink truncate max-w-[14rem] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
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
                  className="text-ink-muted truncate max-w-[8rem]"
                >
                  {live}
                </span>
              )}
              {modelPending && (
                <span
                  aria-label="Model switch pending"
                  title="Switching when the current turn finishes"
                  className="text-warning"
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
            <span className="text-ink-muted">effort</span>
            <span className="font-medium text-ink truncate max-w-[8rem]">
              {reasoningEffort}
            </span>
          </span>
        )}

        {/* Runner host — which fleet member is actually executing this session. */}
        {host && (
          <span className="inline-flex items-center gap-1" aria-label="Runner host">
            <span className="text-ink-muted">host</span>
            <span className="font-medium text-ink truncate max-w-[10rem]">
              {host}
            </span>
          </span>
        )}

        {displayMode === 'native' && (
          <span className="inline-flex items-center gap-1" aria-label="Display" title="Windows open on the host's desktop">
            <span className="text-ink-muted">display</span>
            <span className="font-medium text-ink">desktop</span>
          </span>
        )}

        {/* Remaining context */}
        {(() => {
          const sev = contextSeverity(contextPct)
          const color = sev === 'danger'
            ? 'text-danger'
            : sev === 'warning'
              ? 'text-warning'
              : 'text-ink'
          return (
            <span className={cn('inline-flex items-center gap-1', color)} aria-label="Remaining context">
              <span className="text-ink-muted">ctx</span>
              <span className="font-medium tabular-nums">
                {contextPct != null ? `${contextPct}%` : DASH}
              </span>
              {sev === 'danger' && <span className="opacity-70">· compacts soon</span>}
            </span>
          )
        })()}

        {mcpSlot}

        {/* Account quota — one readout per window the provider reported, so a
            single-allowance provider shows ONE number instead of the same one
            twice under two labels. */}
        {quota && quota.length > 0 && (
          <span className="inline-flex items-center gap-1.5" aria-label="Account quota">
            <span className="text-ink-muted">quota</span>
            {quota.map((w, i) => (
              <Fragment key={w.key}>
                {i > 0 && <span className="text-ink">/</span>}
                <span
                  className="tabular-nums text-ink"
                  title={quotaTitle(w.label, w.resetsAt)}
                  aria-label={`${w.label} usage: ${w.pct}% — ${quotaTitle(w.label, w.resetsAt)}`}
                >
                  {windowLabel(w)} {w.pct}%
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
          onClick={() => onSetMode((pendingMode ?? mode) === 'plan' ? 'default' : 'plan')}
          className={cn(
            'inline-flex items-center rounded px-2 py-0.5 text-xs font-medium min-h-7 border',
            mode === 'plan'
              ? 'bg-accent-wash border-accent text-accent'
              : 'bg-transparent border-edge-strong text-ink-muted',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
          )}
        >
          plan
        </button>
        {pendingMode && (
          <span role="status">{pendingMode === 'plan' ? 'Plan pending' : pendingMode === 'default' ? 'Exit plan pending' : 'Mode change pending'}</span>
        )}
        </div>

        {/* Right cluster: live status (folded-in StatusBar) + export + session
            actions (archive/⋮) + sub-agents toggle. Outside the fold: this is
            what a narrow pane keeps on its one visible row. */}
        {(statusSlot || exportSlot || actionsSlot || agentCount > 0 || bgSummary) && (
          <div className="ml-auto flex items-center gap-2">
            {statusSlot}
            {exportSlot}
            {actionsSlot}
            {agentCount > 0 && (
              <button
                type="button"
                onClick={() => setPanel((p) => (p === 'agents' ? null : 'agents'))}
                aria-expanded={panel === 'agents'}
                aria-label={`${agentCount} sub-agents`}
                className={cn(
                  'inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md min-h-7',
                  'hover:bg-edge active:bg-edge-strong',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                )}
              >
                <span className={cn('h-2 w-2 rounded-full', STATUS_TONE[summaryDot(subAgents)])} aria-hidden="true" />
                <span className="tabular-nums text-ink">{agentCount}</span>
                <span className="text-ink-muted">{agentCount === 1 ? 'agent' : 'agents'}</span>
              </button>
            )}
            {bgSummary && (
              <button
                type="button"
                onClick={() => setPanel((p) => (p === 'background' ? null : 'background'))}
                aria-expanded={panel === 'background'}
                aria-label={`Background work: ${bgSummary}`}
                className={cn('inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md min-h-7',
                  'hover:bg-edge active:bg-edge-strong',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent')}
              >
                <span className={cn('h-2 w-2 rounded-full', statusTone('running'))} aria-hidden="true" />
                <span className="text-ink">{bgSummary}</span>
              </button>
            )}
          </div>
        )}
      </div>

      {/* Sub-agent popover */}
      {panel === 'agents' && agentCount > 0 && (
        <div
          className={cn(
            'absolute right-2 top-full z-sticky mt-1 w-72 max-h-72 overflow-y-auto',
            'rounded-md border border-edge bg-surface shadow-lg px-2 py-1',
          )}
        >
          <SubAgentList sessionId={sessionId} subAgents={subAgents} />
        </div>
      )}

      {/* Background-work popover */}
      {panel === 'background' && bgSummary && (
        <div
          className={cn(
            'absolute right-2 top-full z-sticky mt-1 w-72 max-h-72 overflow-y-auto',
            'rounded-md border border-edge bg-surface shadow-lg px-2 py-1',
          )}
        >
          <BackgroundWorkList sessionId={sessionId} work={backgroundWork} />
        </div>
      )}
    </div>
  )
}
