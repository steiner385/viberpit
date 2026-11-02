/**
 * Rail card: where the session runs — the agent (non-default engines only),
 * the account (switch / unpin, through `AccountSwitcher`), the
 * runner host, the display, the git branch and worktree, and the session's PRs.
 */
import type { AgentEngine, PullRequestInfo } from '@/api/types'
import { engineBadge } from '@/session/modelName'
import { AccountSwitcher } from '../AccountSwitcher'
import { displayReadout } from '../displayFallback'
import { PrList } from '../list/PrList'
import { CopyablePath } from '../transcript/CopyablePath'
import { RailCard, RailField, RailFields } from './RailCard'

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
  prs?: PullRequestInfo[] | null
}

export function EnvironmentCard({
  engine = null, account, accounts, accountPinned, onSelectAccount, onUnpin,
  host = null, displayMode = null, displayFallbackReason = null, branch = null, cwd = null, prs = null,
}: Props) {
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
      {prs && prs.length > 0 && (
        <div className="flex flex-wrap items-center gap-1">
          <PrList prs={prs} />
        </div>
      )}
    </RailCard>
  )
}
