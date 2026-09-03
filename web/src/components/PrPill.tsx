import { GitPullRequest, GitMerge, GitPullRequestClosed, Check, X, Dot, Rocket } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { PullRequestInfo } from '@/api/types'
import { isActionablePrState } from '@/session/prStatus'
import { TONE_TEXT, TONE_WASH, type Tone } from '@/ui/Badge'

// Tone per PR state — mirrors GitHub's semantics (open green, draft neutral,
// merged info, closed danger).
const STATE_TONE: Record<PullRequestInfo['state'], Tone> = {
  open: 'success', draft: 'neutral', merged: 'info', closed: 'danger',
}

const STATE_ICON: Record<PullRequestInfo['state'], typeof GitPullRequest> = {
  open: GitPullRequest,
  draft: GitPullRequest,
  merged: GitMerge,
  closed: GitPullRequestClosed,
}

function ChecksIcon({ checks }: { checks: PullRequestInfo['checks'] }) {
  if (checks === 'passing') return <Check size={11} aria-hidden className="text-success" />
  if (checks === 'failing') return <X size={11} aria-hidden className="text-danger" />
  if (checks === 'pending') return <Dot size={11} aria-hidden className="text-warning" />
  return null
}

// Stages worth surfacing on the pill — 'merged' is already conveyed by the
// state color/icon; '' means unclassified. Show only 'ci', 'ready', 'parked'.
const VISIBLE_STAGES = new Set(['ci', 'ready', 'parked'])

export function PrPill({ pr }: { pr: PullRequestInfo }) {
  const StateIcon = STATE_ICON[pr.state]
  // Checks only matter while a PR is live. GitHub freezes a terminal PR's
  // rollup (a closed PR keeps `checks: 'failing'` forever) but there's nothing
  // to act on, so we don't surface it — the state background (closed=danger,
  // merged=info) already carries the terminal meaning. `data-checks` stays
  // factual for tests/inspection; only the visible icon + label are gated.
  const showChecks = isActionablePrState(pr.state)
  const checksLabel = showChecks && pr.checks !== 'none' ? `, checks ${pr.checks}` : ''
  const deployedLabel = pr.deployed ? ', deployed' : ''
  const stage = pr.stage ?? ''
  const showStage = VISIBLE_STAGES.has(stage)
  const stageLabel = showStage ? `, ${stage}` : ''
  return (
    <a
      href={pr.url}
      target="_blank"
      rel="noopener noreferrer"
      data-state={pr.state}
      data-checks={pr.checks}
      data-deployed={pr.deployed ? 'true' : undefined}
      data-stage={showStage ? stage : undefined}
      onClick={(e) => e.stopPropagation()}
      title={`${pr.title} — ${pr.state}${checksLabel}${deployedLabel}${stageLabel}`}
      aria-label={`Pull request #${pr.number}, ${pr.state}${checksLabel}${deployedLabel}${stageLabel}`}
      className={cn(
        'inline-flex items-center gap-1 text-xs font-medium px-1.5 py-0.5 rounded',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        TONE_WASH[STATE_TONE[pr.state]], TONE_TEXT[STATE_TONE[pr.state]],
      )}
    >
      <StateIcon size={11} aria-hidden />
      <span className="tabular-nums">#{pr.number}</span>
      {showChecks && <ChecksIcon checks={pr.checks} />}
      {pr.deployed && <Rocket size={11} aria-hidden className="text-success" />}
      {showStage && (
        <span className="opacity-70 font-normal">{stage}</span>
      )}
    </a>
  )
}
