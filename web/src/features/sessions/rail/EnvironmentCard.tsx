/**
 * Rail card: where the session runs — the agent (non-default engines only),
 * the account (switch / unpin, through `AccountSwitcher`), the
 * runner host, the display, the git branch and worktree, and the session's PRs —
 * with the PR auto-fix switch beside them (D-14), and the worktree's Discard / Repair
 * (D-19, `WorktreeControls`) and the opt-in sandbox (D-23, `SandboxControls`).
 */
import { useId } from 'react'
import type { AgentEngine, PullRequestInfo } from '@/api/types'
import { engineBadge } from '@/session/modelName'
import { AccountSwitcher } from '../AccountSwitcher'
import { displayReadout } from '../displayFallback'
import { PrList } from '../list/PrList'
import { CopyablePath } from '../transcript/CopyablePath'
import { Switch } from '@/ui/Switch'
import { RailCard, RailField, RailFields } from './RailCard'
import { WorktreeControls, type WorktreeControlsProps } from './WorktreeControls'
import { SandboxControls, type SandboxControlsProps } from './SandboxControls'

/** Shown for a value the server does not know. */
const UNKNOWN = '—'

interface Props {
  engine?: AgentEngine | null
  account: string
  accounts: { name: string; nickname?: string | null }[]
  accountPinned: boolean
  onSelectAccount: (name: string) => void
  onUnpin?: () => void
  /** Fleet host executing the session; null = no host registry entry. */
  host?: string | null
  displayMode?: 'native' | 'virtual' | null
  displayFallbackReason?: string | null
  /** `session.branch`; null when unknown. */
  branch?: string | null
  /** `session.cwd` — the worktree path; null when unknown. */
  cwd?: string | null
  /** The live session's worktree Discard / Repair (D-19); absent, none are offered. */
  worktree?: WorktreeControlsProps
  prs?: PullRequestInfo[] | null
  /** PR Doctor's automatic fixes for this session's PRs (D-14): `enabled` is the
   *  inverse of `prAutofixPaused`. Shown only while the session has a PR — the
   *  draft guard's `owns_pr` is a per-PR check, not a session field. */
  prAutofix?: { enabled: boolean; onChange: (enabled: boolean) => void; pending?: boolean }
  /** The sandbox row (D-23): passed only where the engine reports the capability; the
   *  row still hides unless the owning daemon's facts confirm it. */
  sandbox?: SandboxControlsProps
}

export const PR_AUTOFIX_COPY = "PR Doctor fixes failing checks and conflicts on this session's PRs"


export function EnvironmentCard({
  engine = null, account, accounts, accountPinned, onSelectAccount, onUnpin,
  host = null, displayMode = null, displayFallbackReason = null, branch = null, cwd = null, worktree, prs = null,
  prAutofix, sandbox,
}: Props) {
  const autofixLabelId = useId()
  const autofixCopyId = useId()
  const agent = engineBadge(engine)
  const display = displayReadout(displayMode, displayFallbackReason)
  return (
    <RailCard title="Environment">
      <RailFields>
        {agent && <RailField label="Agent" testId="env-agent">{agent}</RailField>}
        <RailField label="Account">
          <span className="inline-flex min-w-0 items-center gap-1">
            <AccountSwitcher
              account={account}
              accounts={accounts}
              accountPinned={accountPinned}
              onSelectAccount={onSelectAccount}
              onUnpin={onUnpin}
            />
          </span>
        </RailField>
        {host && <RailField label="Host" testId="env-host">{host}</RailField>}
        {display && (
          <RailField label="Display">
            <span title={display.title}>
              <span className="font-medium">{display.value}</span>
              {display.value === 'virtual' && <span className="block text-ink-muted">{display.title}</span>}
            </span>
          </RailField>
        )}
        <RailField label="Branch" testId="env-branch">
          {branch ? <span className="font-mono break-all">{branch}</span> : UNKNOWN}
        </RailField>
        <RailField label="Worktree" testId="env-worktree">
          {cwd ? <span className="break-all"><CopyablePath text={cwd} /></span> : UNKNOWN}
        </RailField>
      </RailFields>
      {sandbox && <SandboxControls {...sandbox} />}
      {cwd && worktree && <WorktreeControls {...worktree} />}
      {prs && prs.length > 0 && (
        <div className="flex flex-wrap items-center gap-1">
          <PrList prs={prs} />
        </div>
      )}
      {prs && prs.length > 0 && prAutofix && (
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div id={autofixLabelId} className="text-ink">PR auto-fix</div>
            <p id={autofixCopyId} className="text-ink-muted">{PR_AUTOFIX_COPY}</p>
          </div>
          <Switch
            checked={prAutofix.enabled}
            onChange={prAutofix.onChange}
            disabled={prAutofix.pending}
            aria-labelledby={autofixLabelId}
            aria-describedby={autofixCopyId}
          />
        </div>
      )}
    </RailCard>
  )
}
