/**
 * Onboarding wizard (PM product layer, phase 2): a VIEW over the onboarding
 * engine (design §5). It renders the next action for the product's current
 * onboarding_state as a small set of controls + a one-line status. It is not
 * authoritative — the backend engine decides; this only presents.
 */
import { Link } from 'react-router-dom'

import {
  useAdvanceLive, useAdvanceMap, useClassify, useOnboarding, useProposeMap,
} from '@/api/hooks'
import type { OnboardingAction, OnboardingCounts, OnboardingMapPr } from '@/api/pm'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { onboardingStateLabel } from '@/features/products/vocab'
import { NAV_BY_ID } from '@/shell/navDefs'

function InlineError({ error, fallback }: { error: unknown; fallback: string }) {
  return (
    <p role="alert" className="pm-error">
      {extractMutationErrorMessage(error, fallback)}
    </p>
  )
}

function AwaitMapPr({ slug, mapPr }: { slug: string; mapPr: OnboardingMapPr | null }) {
  const advance = useAdvanceMap()
  const proposeMap = useProposeMap()
  return (
    <>
      <span className="pm-muted">Review and merge the capability-map PR, then continue.</span>
      {mapPr && (
        <a className="pm-wizard-link" href={mapPr.url} target="_blank" rel="noreferrer">
          Open the capability-map PR ↗
        </a>
      )}
      <button className="pm-wizard-action" disabled={advance.isPending}
              onClick={() => advance.mutate(slug)}>
        {advance.isPending ? 'Continuing…' : 'Map merged → continue'}
      </button>
      <button className="pm-wizard-action" disabled={proposeMap.isPending}
              onClick={() => proposeMap.mutate(slug)}
              title="The synthesis session may have died without opening a PR">
        {proposeMap.isPending ? 'Proposing…' : 'Propose again'}
      </button>
      {advance.isError && <InlineError error={advance.error} fallback="Could not continue" />}
      {proposeMap.isError && (
        <InlineError error={proposeMap.error} fallback="Could not propose the map" />
      )}
    </>
  )
}

function AwaitTriage({ slug, counts }: { slug: string; counts: OnboardingCounts }) {
  const advance = useAdvanceLive()
  const n = counts.pendingProposalCount
  return (
    <>
      <span className="pm-muted">Drain the triage queue, then go live.</span>
      <Link className="pm-wizard-link"
            to={`${NAV_BY_ID.triage.path}?projectSlug=${encodeURIComponent(slug)}`}>
        {n === 0 ? 'Open the Triage inbox →' : `Review ${n} proposal${n === 1 ? '' : 's'} in Triage →`}
      </Link>
      <button className="pm-wizard-action" disabled={advance.isPending}
              onClick={() => advance.mutate(slug)}>
        {advance.isPending ? 'Going live…' : 'Go live'}
      </button>
      {advance.isError && <InlineError error={advance.error} fallback="Could not go live" />}
    </>
  )
}

function ActionButton(
  { action, slug, counts, mapPr }:
  { action: OnboardingAction; slug: string; counts: OnboardingCounts; mapPr: OnboardingMapPr | null },
) {
  const proposeMap = useProposeMap()
  const classify = useClassify()
  switch (action.type) {
    case 'propose_map':
      return (
        <>
          <button className="pm-wizard-action" disabled={proposeMap.isPending}
                  onClick={() => proposeMap.mutate(slug)}>
            {proposeMap.isPending ? 'Proposing…' : 'Propose capability map'}
          </button>
          {proposeMap.isError && (
            <InlineError error={proposeMap.error} fallback="Could not propose the map" />
          )}
        </>
      )
    case 'classify':
      return (
        <>
          <button className="pm-wizard-action" disabled={classify.isPending}
                  onClick={() => classify.mutate(slug)}>
            {classify.isPending ? 'Classifying…' : 'Classify backlog'}
          </button>
          {classify.isError && (
            <InlineError error={classify.error} fallback="Could not start classification" />
          )}
        </>
      )
    case 'await_backfill':
      return <span className="pm-muted">Waiting for backlog sync to finish…</span>
    case 'await_map_pr':
      return <AwaitMapPr slug={slug} mapPr={mapPr} />
    case 'await_triage':
      return <AwaitTriage slug={slug} counts={counts} />
    case 'register':
      return <span className="pm-muted">No repository is attached to this product.</span>
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
        <span className="pm-wizard-state">{onboardingStateLabel(data.state)}</span>
        <span className="pm-muted">
          {data.counts.featureCount} capabilities · {data.counts.openUnclassifiedCount} open items ·{' '}
          {data.counts.pendingProposalCount} awaiting triage
        </span>
      </div>
      <ActionButton action={data.action} slug={slug} counts={data.counts} mapPr={data.mapPr} />
    </div>
  )
}
