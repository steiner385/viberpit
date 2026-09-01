/**
 * Onboarding wizard (PM product layer, phase 2): a VIEW over the onboarding
 * engine (design §5). It renders the next action for the project's current
 * onboarding_state as a single actionable button + a one-line status. It is not
 * authoritative — the backend engine decides; this only presents.
 */
import { useClassify, useOnboarding, useProposeMap } from '@/api/hooks'
import type { OnboardingAction } from '@/api/pm'

const STATE_LABEL: Record<string, string> = {
  registered: 'Registered', syncing: 'Syncing backlog',
  map_proposed: 'Feature map proposed', map_merged: 'Feature map merged',
  classifying: 'Classifying backlog', live: 'Live',
}

function ActionButton({ action, slug }: { action: OnboardingAction; slug: string }) {
  const proposeMap = useProposeMap()
  const classify = useClassify()
  switch (action.type) {
    case 'propose_map':
      return (
        <button className="pm-wizard-action" disabled={proposeMap.isPending}
                onClick={() => proposeMap.mutate(slug)}>
          {proposeMap.isPending ? 'Proposing…' : 'Propose feature map'}
        </button>
      )
    case 'classify':
      return (
        <button className="pm-wizard-action" disabled={classify.isPending}
                onClick={() => classify.mutate(slug)}>
          {classify.isPending ? 'Classifying…' : 'Classify backlog'}
        </button>
      )
    case 'await_backfill':
      return <span className="pm-muted">Waiting for backlog sync to finish…</span>
    case 'await_map_pr':
      return <span className="pm-muted">Review &amp; merge the feature-map PR, then advance.</span>
    case 'await_triage':
      return <span className="pm-muted">Drain the triage queue below, then advance.</span>
    case 'register':
      return <span className="pm-muted">Add a repo to begin.</span>
    case 'needs_human':
      return <span className="pm-warn">Needs attention: {action.reason}</span>
    case 'live':
      return null
    default:
      return null
  }
}

export function OnboardingWizard({ slug }: { slug: string }) {
  const { data, isLoading } = useOnboarding(slug)
  if (isLoading || !data) return null
  if (data.state === 'live') return null
  return (
    <div className="pm-wizard">
      <div className="pm-wizard-head">
        <span className="pm-wizard-state">{STATE_LABEL[data.state] ?? data.state}</span>
        <span className="pm-muted">
          {data.counts.featureCount} features · {data.counts.openUnclassifiedCount} open items ·{' '}
          {data.counts.pendingProposalCount} awaiting triage
        </span>
      </div>
      <ActionButton action={data.action} slug={slug} />
    </div>
  )
}
