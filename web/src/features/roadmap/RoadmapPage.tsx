import { Link, useParams } from 'react-router-dom'
import { useRoadmapMap } from '@/api/hooks'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { EmptyState } from '@/ui/EmptyState'
import { Banner } from '@/ui/Banner'
import { Breadcrumb, type BreadcrumbItem } from '@/ui/Breadcrumb'
import { PRODUCT, ROADMAP } from '@/features/products/vocab'
import { productPath, roadmapOpenItem, roadmapPath } from './roadmapNavState'
import { findCapability, findProduct } from './lookup'
import { PortfolioDetail } from './detail/PortfolioDetail'

/** `/roadmap`, `/roadmap/:productSlug`, `/roadmap/:productSlug/capabilities/:capSlug` —
 *  one routed page, three detail levels chosen by the params (spec §7). */
export function RoadmapPage() {
  const item = roadmapOpenItem(useParams())
  // An initiative is never open on THESE routes (it has its own page), so the
  // rest of the component reasons about the product/capability item alone.
  const opened = item && item.kind !== 'initiative' ? item : null
  const { data: map, isLoading, isError } = useRoadmapMap()
  const product = opened ? findProduct(map, opened.productSlug) : null
  const capability = opened?.kind === 'capability' && product ? findCapability(product, opened.capSlug) : null

  // The address points at something the map does not contain — a renamed slug or
  // a stale bookmark. Gated on the map having ARRIVED: a failed fetch also leaves
  // `product` null with `isLoading` false, and that is a broken connection, not a
  // renamed product.
  const missing = map !== undefined && opened !== null
    && (product === null || (opened.kind === 'capability' && capability === null))

  const crumbs: BreadcrumbItem[] = [{ label: ROADMAP.label, to: roadmapPath() }]
  // Annotated: `ROADMAP.label` is a const literal, so an inferred `title` could
  // never be reassigned to a product or capability name.
  let title: string = ROADMAP.label
  if (opened) {
    // The slug is the fallback title, so a deep link to a product the map has
    // not delivered yet still names something rather than rendering blank.
    title = product?.name ?? opened.productSlug
    crumbs.push({ label: title, to: opened.kind === 'capability' ? productPath(opened.productSlug) : undefined })
  }
  if (opened?.kind === 'capability') {
    title = capability?.title ?? opened.capSlug
    crumbs.push({ label: title })
  }

  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="standard">
      <PageHeader title={title} meta={crumbs.length > 1 ? <Breadcrumb items={crumbs} /> : undefined} />
      <PageContent busy={isLoading && !map}>
        {/* Errors are a Banner, first child of the content region. Same copy as the
            list column, which fails from the same query. */}
        {isError && <Banner tone="danger" title="Could not load the roadmap." />}
        {/* Nothing open: the portfolio IS the page, not a "pick something" prompt. */}
        {map && !opened && <PortfolioDetail map={map} />}
        {/* Product and capability detail arrive with the next tasks; until then the
            open item is named rather than left blank. */}
        {map && opened && !missing && <EmptyState title={title} />}
        {missing && (
          <EmptyState
            title="Not found"
            description={`Nothing on the ${ROADMAP.label.toLowerCase()} matches this address. It may have been renamed.`}
            action={<Link className="btn-ghost" to={roadmapPath()}>Back to the {ROADMAP.label.toLowerCase()}</Link>}
          />
        )}
        {/* No map and not loading: the region always says something. The Banner
            above carries the reason when there is one; the map re-fetches on its
            own interval, so this state clears itself. */}
        {!map && !isLoading && (isError
          ? <EmptyState title={`No ${ROADMAP.label.toLowerCase()} to show`} description="It will fill in as soon as the map loads again." />
          : <EmptyState title={PRODUCT.empty} />)}
      </PageContent>
    </div>
  )
}
