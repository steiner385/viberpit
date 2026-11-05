// `/roadmap/:productSlug/milestones` — a product's release-milestone ladder
// (spec 2026-11-05 §9). At a glance first: the header strip says which milestone
// we are in, what is left in its gate, what is in flight, how many asks need you
// and how fresh the numbers are; the ladder and the side column (Needs you,
// Numbers, Decisions) sit below it. Every write answers with the full view, which
// replaces the cached one. Its own route rather than an 8th product tab
// (docs/PAGE-TABS.md §4). Export PDF prints this live view as one landscape page
// (spec M6): print.css lays it out, hides the shell and every control, and the
// print-only header and stamp say what and when.
import { type CSSProperties, useEffect, useId, useRef } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useProject } from '@/api/hooks'
import { apiErrorCode } from '@/api/client'
import { milestoneErrorText, useAdvanceMilestone, useMilestones, useRefreshMilestones } from '@/api/milestones'
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
import { NotesPanel } from './NotesPanel'
import { NumbersPanel } from './NumbersPanel'
import { PrintStamp } from './PrintStamp'
import {
  MILESTONES_FILE, MILESTONES_REFRESH_ID, currentMilestone, gateLine, loadedView, milestoneTitle,
} from './format'
import './print.css'

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
  // THE advance mutation for Start and every card's dialog, owned here so its
  // error outlives the card: a 409 `stale_current` refetches, the card stops being
  // current and its dialog unmounts.
  const advance = useAdvanceMilestone(productSlug)
  const offId = useId()

  const productName = project?.name ?? productSlug
  const repo = project?.repos.find((r) => r.isPrimary)?.repo ?? project?.repos[0]?.repo ?? null
  const view = loadedView(milestones.data)
  const ladder = view?.ladder ?? null
  const current = view ? currentMilestone(view) : null
  // The rung an Advance enters: the lowest live upcoming one (the server's rule).
  const upcoming = view?.milestones.find((m) => m.state === 'upcoming') ?? null
  const canStart = !!view && view.ladder.currentKey === null && !view.milestones.some((m) => m.state === 'done') && upcoming !== null
  // The strip says what Start's failure was, and a stale advance from anywhere; a
  // dialog's other refusals stay in the dialog.
  const stale = apiErrorCode(advance.error) === 'stale_current'
  const advanceError = stale || advance.variables?.expectedCurrent === null ? milestoneErrorText(advance.error) : null
  const asksOpen = ladder?.asksOpen ?? 0
  const outcome = refreshOutcome(refresh.data)

  // The outcome line is transient: it clears when newer numbers land, and a 429's
  // "Try again in N s" clears once N seconds have passed.
  const { reset } = refresh
  const updatedAt = ladder?.updatedAt ?? null
  const seenUpdatedAt = useRef(updatedAt)
  useEffect(() => {
    if (updatedAt === seenUpdatedAt.current) return
    seenUpdatedAt.current = updatedAt
    if (refresh.data) reset()
  }, [updatedAt, refresh.data, reset])
  const retryAfter = refresh.data && 'retryAfter' in refresh.data ? refresh.data.retryAfter : null
  useEffect(() => {
    if (retryAfter === null) return
    const t = window.setTimeout(reset, retryAfter * 1000)
    return () => window.clearTimeout(t)
  }, [retryAfter, refresh.data, reset])

  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="standard" data-milestones-print>
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
                <Banner
                  tone="danger"
                  title={`The milestone definition file has an error. ${
                    view.milestones.length > 0 ? 'Showing the last good version.' : 'No version of it has parsed yet.'}`}
                >
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
              {ladder.ladderComplete && (
                <Banner tone="success" title="Ladder complete">
                  Every milestone is done. Add a new milestone to the definition file to keep going.
                </Banner>
              )}
              {!ladder.autoRefresh && (
                <div data-print="hide">
                  <Banner
                    tone="info"
                    title={<span id={offId}>{AUTO_REFRESH_OFF}</span>}
                    actions={
                      <Link to={automationPath(MILESTONES_REFRESH_ID)} className="text-sm text-accent hover:underline">
                        Open Milestones Refresh
                      </Link>
                    }
                  />
                </div>
              )}

              {/* The printed page's header: what this is and when (the page's own
                  header, with its breadcrumb, does not print). */}
              <div data-print="head">
                <p className="font-semibold text-ink">
                  {productName}{ladder.title ? ` · ${ladder.title}` : ''}
                </p>
                <PrintStamp product={productName} />
              </div>

              <section aria-label="Where we are" className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-md border border-edge bg-surface px-4 py-3 text-sm">
                <p className="font-semibold text-ink">
                  {current ? `You are here: ${milestoneTitle(current)}` : 'No current milestone'}
                </p>
                {current && <p className="text-ink">{gateLine(current.gateSummary)}</p>}
                {current && <p className="text-ink">{current.leaves.inFlight} in flight</p>}
                {asksOpen > 0 && (
                  <p className="font-medium text-warning">{asksOpen} {asksOpen === 1 ? 'ask needs' : 'asks need'} you</p>
                )}
                {canStart && (
                  <Button size="sm" onClick={() => advance.mutate({ expectedCurrent: null })} loading={advance.isPending}>
                    Start
                  </Button>
                )}
                {advanceError && <p role="alert" className="text-danger">{advanceError}</p>}
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
                  <Button size="sm" variant="secondary" onClick={() => window.print()}>
                    Export PDF
                  </Button>
                  {outcome && ladder.autoRefresh && (
                    <span role="status" data-print="hide" className="text-xs text-ink-muted">{outcome}</span>
                  )}
                </div>
              </section>

              <div data-print="body" className="grid gap-4 2xl:grid-cols-[minmax(0,1fr)_18rem]">
                <ol
                  aria-label="Milestones"
                  data-print="ladder"
                  // Print sets one row of N from `--print-cols` (print.css).
                  style={{ gridTemplateColumns: LADDER_COLUMNS, '--print-cols': view.milestones.length } as CSSProperties}
                  className="grid gap-3"
                >
                  {view.milestones.map((m) => (
                    <MilestoneCard key={m.key} milestone={m} slug={productSlug} next={upcoming} advance={advance}
                      currentRemoved={view.ladder.currentRemoved} />
                  ))}
                </ol>
                {/* Spec §9 order, kept when it stacks under the ladder below 2xl. */}
                <div data-print="side" className="flex min-w-0 flex-col gap-6">
                  <NotesPanel kind="ask" slug={productSlug} view={view} />
                  <NumbersPanel numbers={view.numbers} slug={productSlug} />
                  <NotesPanel kind="decision" slug={productSlug} view={view} />
                </div>
              </div>
            </>
          )}
        </div>
      </PageContent>
    </div>
  )
}
