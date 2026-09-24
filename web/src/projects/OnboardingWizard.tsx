/**
 * Onboarding wizard (PM product layer, phase 2): a VIEW over the onboarding
 * engine (design §5). It renders the next action for the product's current
 * onboarding_state as a small set of controls + a one-line status. It is not
 * authoritative — the backend engine decides; this only presents.
 */
import { Link } from 'react-router-dom'

import {
  useAdvanceLive, useAdvanceMap, useClassify, useOnboarding, useProposeConstitution, useProposeMap,
} from '@/api/hooks'
import type { OnboardingAction, OnboardingCounts, OnboardingMapPr } from '@/api/pm'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { ROADMAP, onboardingStateLabel } from '@/features/products/vocab'
import { inboxPath } from '@/features/roadmap/roadmapNavState'
import { Button } from '@/ui/Button'

function InlineError({ error, fallback }: { error: unknown; fallback: string }) {
  return (
    <p role="alert" className="text-sm text-danger">
      {extractMutationErrorMessage(error, fallback)}
    </p>
  )
}

function AwaitMapPr({ slug, mapPr }: { slug: string; mapPr: OnboardingMapPr | null }) {
  const advance = useAdvanceMap()
  const proposeMap = useProposeMap()
  return (
    <>
      <span className="text-sm text-ink-muted">Review and merge the capability-map PR, then continue.</span>
      {mapPr && (
        <a className="text-sm text-accent underline" href={mapPr.url} target="_blank" rel="noreferrer">
          Open the capability-map PR ↗
        </a>
      )}
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" size="sm" disabled={advance.isPending}
                onClick={() => advance.mutate(slug)}>
          {advance.isPending ? 'Continuing…' : 'Map merged → continue'}
        </Button>
        <Button variant="secondary" size="sm" disabled={proposeMap.isPending}
                onClick={() => proposeMap.mutate(slug)}
                title="The synthesis session may have died without opening a PR">
          {proposeMap.isPending ? 'Proposing…' : 'Propose again'}
        </Button>
      </div>
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
      <span className="text-sm text-ink-muted">Drain the triage queue, then go live.</span>
      {/* The queue is the Roadmap section's Inbox scope, narrowed to this
          product (finish-line row 3) — there is no Triage page to send them to. */}
      <Link className="text-sm text-accent underline" to={inboxPath({ product: slug })}>
        {n === 0
          ? `Open the ${ROADMAP.inbox} →`
          : `Review ${n} proposal${n === 1 ? '' : 's'} in the ${ROADMAP.inbox} →`}
      </Link>
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" size="sm" disabled={advance.isPending}
                onClick={() => advance.mutate(slug)}>
          {advance.isPending ? 'Going live…' : 'Go live'}
        </Button>
      </div>
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
          <Button variant="secondary" size="sm" className="self-start" disabled={proposeMap.isPending}
                  onClick={() => proposeMap.mutate(slug)}>
            {proposeMap.isPending ? 'Proposing…' : 'Propose capability map'}
          </Button>
          {proposeMap.isError && (
            <InlineError error={proposeMap.error} fallback="Could not propose the map" />
          )}
        </>
      )
    case 'classify':
      return (
        <>
          <Button variant="secondary" size="sm" className="self-start" disabled={classify.isPending}
                  onClick={() => classify.mutate(slug)}>
            {classify.isPending ? 'Classifying…' : 'Classify backlog'}
          </Button>
          {classify.isError && (
            <InlineError error={classify.error} fallback="Could not start classification" />
          )}
        </>
      )
    case 'await_backfill':
      return <span className="text-sm text-ink-muted">Waiting for backlog sync to finish…</span>
    case 'await_map_pr':
      return <AwaitMapPr slug={slug} mapPr={mapPr} />
    case 'await_triage':
      return <AwaitTriage slug={slug} counts={counts} />
    case 'register':
      return <span className="text-sm text-ink-muted">No repository is attached to this product.</span>
    case 'needs_human':
      return <span className="text-sm text-warning">Needs attention: {action.reason}</span>
    case 'live':
      return null
    default:
      return null
  }
}

// issue #2259 (item 2 of #2165): offered independently of the required
// onboarding ladder above — once the map is merged and the repo's governance
// document has an actual gap (`OnboardingView.offerConstitution`).
function ConstitutionOffer({ slug }: { slug: string }) {
  const propose = useProposeConstitution()
  return (
    <>
      <span className="text-sm text-ink-muted">
        This repository has no ratified constitution yet.
      </span>
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" size="sm" disabled={propose.isPending}
                onClick={() => propose.mutate(slug)}>
          {propose.isPending ? 'Proposing…' : 'Propose constitution'}
        </Button>
      </div>
      {propose.isError && (
        <InlineError error={propose.error} fallback="Could not propose a constitution" />
      )}
    </>
  )
}

export function OnboardingWizard({ slug }: { slug: string }) {
  const { data, isLoading } = useOnboarding(slug)
  if (isLoading || !data) return null
  if (data.state === 'live') {
    if (!data.offerConstitution) return null
    return (
      <div className="mt-2 flex flex-col gap-2 rounded-md border border-edge bg-surface p-3">
        <ConstitutionOffer slug={slug} />
      </div>
    )
  }
  return (
    <div className="mt-2 flex flex-col gap-2 rounded-md border border-edge bg-surface p-3">
      <div className="flex flex-col gap-1">
        <span className="font-medium text-ink">{onboardingStateLabel(data.state)}</span>
        <span className="text-sm text-ink-muted">
          {data.counts.featureCount} capabilities · {data.counts.openUnclassifiedCount} open items ·{' '}
          {data.counts.pendingProposalCount} awaiting triage
        </span>
      </div>
      <ActionButton action={data.action} slug={slug} counts={data.counts} mapPr={data.mapPr} />
      {data.offerConstitution && <ConstitutionOffer slug={slug} />}
    </div>
  )
}
