import { useParams } from 'react-router-dom'
import { useRoadmapMap } from '@/api/hooks'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { EmptyState } from '@/ui/EmptyState'
import { Breadcrumb, type BreadcrumbItem } from '@/ui/Breadcrumb'
import { ROADMAP } from '@/features/products/vocab'
import { productPath, roadmapOpenItem, roadmapPath } from './roadmapNavState'
import { findCapability, findProduct } from './lookup'

/** `/roadmap`, `/roadmap/:productSlug`, `/roadmap/:productSlug/capabilities/:capSlug` —
 *  one routed page, three detail levels chosen by the params (spec §7). */
export function RoadmapPage() {
  const item = roadmapOpenItem(useParams())
  const { data: map, isLoading } = useRoadmapMap()
  const product = item && item.kind !== 'initiative' ? findProduct(map, item.productSlug) : null
  const capability = item?.kind === 'capability' && product ? findCapability(product, item.capSlug) : null

  const crumbs: BreadcrumbItem[] = [{ label: ROADMAP.label, to: roadmapPath() }]
  // Annotated: `ROADMAP.label` is a const literal, so an inferred `title` could
  // never be reassigned to a product or capability name.
  let title: string = ROADMAP.label
  if (item && item.kind !== 'initiative') {
    // The slug is the fallback title, so a deep link to a product the map has
    // not delivered yet still names something rather than rendering blank.
    title = product?.name ?? item.productSlug
    crumbs.push({ label: title, to: item.kind === 'capability' ? productPath(item.productSlug) : undefined })
  }
  if (item?.kind === 'capability') {
    title = capability?.title ?? item.capSlug
    crumbs.push({ label: title })
  }

  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="standard">
      <PageHeader title={title} meta={crumbs.length > 1 ? <Breadcrumb items={crumbs} /> : undefined} />
      <PageContent busy={isLoading && !map}>
        <EmptyState title={item ? 'Loading…' : 'Select a product from the list'} />
      </PageContent>
    </div>
  )
}
