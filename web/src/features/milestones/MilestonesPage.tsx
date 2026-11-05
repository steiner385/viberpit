// `/roadmap/:productSlug/milestones` — a product's release-milestone ladder,
// read-only in Phase 1 (spec 2026-11-05 §9). At a glance first: the header
// strip says which milestone we are in, what is left in its gate, what is in
// flight and how fresh the numbers are; the ladder and the Numbers panel sit
// below it. Its own route rather than an 8th product tab (docs/PAGE-TABS.md §4).
import { useId } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMilestones, useProject, useRefreshMilestones } from '@/api/hooks'
import type { RefreshMilestonesResult } from '@/api/milestones'
import { automationPath } from '@/features/delivery/deliveryNavState'
import { ROADMAP } from '@/features/products/vocab'
import { productPath, roadmapPath } from '@/features/roadmap/roadmapNavState'
import { fileUrl } from '@/lib/githubUrl'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { Banner } from '@/ui/Banner'
import { Breadcrumb } from '@/ui/Breadcrumb'
import { Button } from '@/ui/Button'
import { EmptyState } from '@/ui/EmptyState'
import { PageContent } from '@/ui/PageContent'
import { PageHeader } from '@/ui/PageHeader'
import { QueryFailureBanner } from '@/ui/QueryFailureBanner'
import { Skeleton } from '@/ui/Skeleton'
import { MilestoneCard } from './MilestoneCard'
import { NumbersPanel } from './NumbersPanel'
import {
  MILESTONES_FILE, MILESTONES_REFRESH_ID, currentMilestone, gateLine, loadedView, milestoneTitle,
} from './format'

const AUTO_REFRESH_OFF =
  'Automatic refresh is off. Turn on Milestones Refresh in Automations to keep these numbers live.'

/** At most four rungs per row, none narrower than 15rem: each track is at least a
 *  quarter of the row (so a fifth never fits) and at least 15rem (so a narrow row
 *  wraps instead of squeezing); `min(100%, …)` keeps a single card inside a phone. */
const LADDER_COLUMNS = 'repeat(auto-fit, minmax(min(100%, max(15rem, (100% - 3 * 0.75rem) / 4)), 1fr))'

function refreshOutcome(r: RefreshMilestonesResult | undefined): string | null {
  if (!r) return null
  if (r.refreshed) return 'Refresh requested'
  if ('retryAfter' in r) return `Try again in ${r.retryAfter} s`
  return AUTO_REFRESH_OFF
}

export function MilestonesPage() {
  const { productSlug = '' } = useParams()
  const milestones = useMilestones(productSlug)
  const { data: project } = useProject(productSlug, { poll: false })
  const refresh = useRefreshMilestones(productSlug)
  const offId = useId()

  const productName = project?.name ?? productSlug
  const repo = project?.repos.find((r) => r.isPrimary)?.repo ?? project?.repos[0]?.repo ?? null
  const view = loadedView(milestones.data)
  const ladder = view?.ladder ?? null
  const current = view ? currentMilestone(view) : null
  const outcome = refreshOutcome(refresh.data)


  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="standard">
      <PageHeader
        title="Milestones"
        documentTitle={`Milestones · ${productName}`}
        meta={
          <Breadcrumb
            items={[
              { label: ROADMAP.label, to: roadmapPath() },
              { label: productName, to: productPath(productSlug) },
              { label: 'Milestones' },
            ]}
          />
        }
      />
      <PageContent busy={milestones.isLoading}>
        <div className="flex flex-col gap-4">
          {milestones.isError && (
            <QueryFailureBanner
              message="Could not load milestones."
              hasData={!!milestones.data}
              onRetry={() => void milestones.refetch()}
            />
          )}
          {milestones.isLoading && !milestones.data && <Skeleton lines={5} />}
          {milestones.data && !ladder && !milestones.isError && (
            <EmptyState
              title="No milestones yet"
              description={<>Add <code>{MILESTONES_FILE}</code> to this product&apos;s primary repository to define its release ladder.</>}
            />
          )}
          {view && ladder && (
            <>
              {ladder.parseError && (
                <Banner tone="danger" title="The milestone definition file has an error. Showing the last good version.">
                  {ladder.parseError}
                  {ladder.parseErrorSha && (
                    <>
                      {' · at '}
                      {repo && ladder.sourcePath
                        ? <a
                            href={fileUrl(repo, ladder.parseErrorSha, ladder.sourcePath)}
                            target="_blank"
                            rel="noreferrer"
                            className="font-mono text-accent hover:underline"
                          >
                            {ladder.parseErrorSha.slice(0, 7)}
                          </a>
                        : <code>{ladder.parseErrorSha.slice(0, 7)}</code>}
                    </>
                  )}
                </Banner>
              )}
              {ladder.currentRemoved && (
                <Banner tone="warning" title="The current milestone was removed from the definition file" />
              )}
              {!ladder.autoRefresh && (
                <Banner
                  tone="info"
                  title={<span id={offId}>{AUTO_REFRESH_OFF}</span>}
                  actions={
                    <Link to={automationPath(MILESTONES_REFRESH_ID)} className="text-sm text-accent hover:underline">
                      Open Milestones Refresh
                    </Link>
                  }
                />
              )}

              <section aria-label="Where we are" className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-md border border-edge bg-surface px-4 py-3 text-sm">
                <p className="font-semibold text-ink">
                  {current ? `You are here: ${milestoneTitle(current)}` : 'No current milestone'}
                </p>
                {current && <p className="text-ink">{gateLine(current.gateSummary)}</p>}
                {current && <p className="text-ink">{current.leaves.inFlight} in flight</p>}
                <div className="flex flex-wrap items-center gap-2 sm:ml-auto">
                  <p className="text-ink-muted">
                    {isValidTimestamp(ladder.updatedAt) ? `Updated ${relTime(ladder.updatedAt)}` : 'Not measured yet'}
                  </p>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => refresh.mutate()}
                    loading={refresh.isPending}
                    disabled={!ladder.autoRefresh}
                    aria-describedby={!ladder.autoRefresh ? offId : undefined}
                  >
                    Refresh
                  </Button>
                  {outcome && ladder.autoRefresh && (
                    <span role="status" className="text-xs text-ink-muted">{outcome}</span>
                  )}
                </div>
              </section>

              <div className="grid gap-4 2xl:grid-cols-[minmax(0,1fr)_16rem]">
                <ol
                  aria-label="Milestones"
                  style={{ gridTemplateColumns: LADDER_COLUMNS }}
                  className="grid gap-3"
                >
                  {view.milestones.map((m) => <MilestoneCard key={m.key} milestone={m} />)}
                </ol>
                <NumbersPanel numbers={view.numbers} />
              </div>
            </>
          )}
        </div>
      </PageContent>
    </div>
  )
}
