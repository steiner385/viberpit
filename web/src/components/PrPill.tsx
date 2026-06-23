import { GitPullRequest, GitMerge, GitPullRequestClosed, Check, X, Dot } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { PullRequestInfo } from '@/api/types'

// Color per PR state — mirrors GitHub's semantics (open green, draft gray,
// merged purple, closed red).
const STATE_STYLES: Record<PullRequestInfo['state'], string> = {
  open: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  draft: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
  merged: 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
  closed: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
}

const STATE_ICON: Record<PullRequestInfo['state'], typeof GitPullRequest> = {
  open: GitPullRequest,
  draft: GitPullRequest,
  merged: GitMerge,
  closed: GitPullRequestClosed,
}

function ChecksIcon({ checks }: { checks: PullRequestInfo['checks'] }) {
  if (checks === 'passing') return <Check size={11} aria-hidden className="text-green-600 dark:text-green-400" />
  if (checks === 'failing') return <X size={11} aria-hidden className="text-red-600 dark:text-red-400" />
  if (checks === 'pending') return <Dot size={11} aria-hidden className="text-amber-500" />
  return null
}

export function PrPill({ pr }: { pr: PullRequestInfo }) {
  const StateIcon = STATE_ICON[pr.state]
  const checksLabel = pr.checks === 'none' ? '' : `, checks ${pr.checks}`
  return (
    <a
      href={pr.url}
      target="_blank"
      rel="noopener noreferrer"
      data-state={pr.state}
      data-checks={pr.checks}
      onClick={(e) => e.stopPropagation()}
      title={`${pr.title} — ${pr.state}${checksLabel}`}
      aria-label={`Pull request #${pr.number}, ${pr.state}${checksLabel}`}
      className={cn(
        'inline-flex items-center gap-1 text-xs font-medium px-1.5 py-0.5 rounded',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
        STATE_STYLES[pr.state],
      )}
    >
      <StateIcon size={11} aria-hidden />
      <span className="tabular-nums">#{pr.number}</span>
      <ChecksIcon checks={pr.checks} />
    </a>
  )
}
