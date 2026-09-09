import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useProject, useRoadmapMap, useSyncProject } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { EmptyState } from '@/ui/EmptyState'
import { Banner } from '@/ui/Banner'
import { Button } from '@/ui/Button'
import { Breadcrumb, type BreadcrumbItem } from '@/ui/Breadcrumb'
import { INITIATIVE, PRODUCT, ROADMAP } from '@/features/products/vocab'
import { capabilityPath, productPath, roadmapOpenItem, roadmapPath } from './roadmapNavState'
import { capabilityTrail, findCapability, findProduct } from './lookup'
import { PortfolioDetail } from './detail/PortfolioDetail'
import { ProductDetail } from './detail/ProductDetail'
import { CapabilityDetail } from './detail/CapabilityDetail'
import { NewInitiativeDialog } from './detail/NewInitiativeDialog'
import { NewProductDialog } from './detail/NewProductDialog'

/** `/roadmap`, `/roadmap/:productSlug`, `/roadmap/:productSlug/capabilities/:capSlug` —
 *  one routed page, three detail levels chosen by the params (spec §7). */
export function RoadmapPage() {
  const item = roadmapOpenItem(useParams())
  // An initiative is never open on THESE routes (it has its own page), so the
  // rest of the component reasons about the product/capability item alone.
  const opened = item && item.kind !== 'initiative' ? item : null
  // `poll: false`: the shell's list column observes the same cache entry and
  // owns the 15 s interval. Two polling observers of one key are two requests.
  const { data: map, isLoading, isError } = useRoadmapMap({ poll: false })
  const sync = useSyncProject()
  const [creating, setCreating] = useState(false)
  const [registering, setRegistering] = useState(false)
  const product = opened ? findProduct(map, opened.productSlug) : null
  const capability = opened?.kind === 'capability' && product ? findCapability(product, opened.capSlug) : null
  // Shares `QK.project(slug)` with `ProductDetail` and the new-initiative
  // dialog, so the three of them are one fetch. Only the repo a feature doc is
  // committed in is needed here.
  const { data: project, isError: projectError } = useProject(product?.slug ?? null)
  const primaryRepo = project?.repos.find((r) => r.isPrimary)?.repo ?? undefined

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
    // Every ancestor is a link, so a nested capability can be walked back up one
    // level at a time. The last node of the trail IS the open capability, which
    // Breadcrumb renders as the current page.
    const trail = product ? capabilityTrail(product, opened.capSlug) : []
    for (const node of trail.slice(0, -1)) {
      crumbs.push({ label: node.title ?? node.slug, to: capabilityPath(opened.productSlug, node.slug) })
    }
    crumbs.push({ label: title })
  }

  // Product-level actions only: a product is the thing that syncs and the thing
  // an initiative belongs to. Both need the product to actually be on the map —
  // `Sync` on a slug the map does not have would post to nothing.
  const productActions = opened?.kind === 'product' && product
    ? (
      <>
        <Button size="sm" variant="ghost" onClick={() => sync.mutate(product.slug)} loading={sync.isPending}>
          Sync
        </Button>
        <Button size="sm" onClick={() => setCreating(true)}>+ {INITIATIVE.singular}</Button>
      </>
    )
    // Nothing open is the PORTFOLIO, and registering a product is the portfolio's
    // action — the entrance to the whole roadmap, and the only one (the Products
    // page this section replaced carried the only other new-product form).
    : !opened
      ? <Button size="sm" onClick={() => setRegistering(true)}>+ {PRODUCT.singular}</Button>
      : undefined

  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="standard">
      <PageHeader
        title={title}
        meta={crumbs.length > 1 ? <Breadcrumb items={crumbs} /> : undefined}
        actions={productActions}
      />
      <PageContent busy={isLoading && !map}>
        {/* Errors are a Banner, first child of the content region. Same copy as the
            list column, which fails from the same query. */}
        {isError && <Banner tone="danger" className="mb-4" title="Could not load the roadmap." />}
        {/* Sync opts out of the global error toast (`PM_MUTATION_META`), so
            without this a refused sync said nothing at all — the button just
            stopped spinning. Same idiom and position as InitiativePage's
            failed Advance. */}
        {sync.isError && (
          <Banner
            tone="danger"
            className="mb-4"
            title={extractMutationErrorMessage(sync.error, 'Could not request sync')}
          />
        )}
        {/* Nothing open: the portfolio IS the page, not a "pick something" prompt. */}
        {map && !opened && <PortfolioDetail map={map} />}
        {/* A non-null `capability` is exactly the capability level: it is only
            resolved for a capability address, and a capability address that
            resolves to nothing is `missing` above. */}
        {map && !missing && product && (
          capability
            ? (
              <CapabilityDetail
                product={product}
                capability={capability}
                primaryRepo={primaryRepo}
                projectError={projectError}
                onNewInitiative={() => setCreating(true)}
              />
            )
            : <ProductDetail product={product} />
        )}
        {missing && (
          <EmptyState
            title="Not found"
            description={`Nothing on the ${ROADMAP.label.toLowerCase()} matches this address. It may have been renamed.`}
            action={(
              <Link className="text-accent hover:underline" to={roadmapPath()}>
                Back to the {ROADMAP.label.toLowerCase()}
              </Link>
            )}
          />
        )}
        {/* No map and not loading: the region always says something. The Banner
            above carries the reason when there is one; the map re-fetches on its
            own interval, so this state clears itself. */}
        {!map && !isLoading && (isError
          ? <EmptyState title={`No ${ROADMAP.label.toLowerCase()} to show`} description="It will fill in as soon as the map loads again." />
          : (
            <EmptyState
              title={PRODUCT.empty}
              action={<Button onClick={() => setRegistering(true)}>+ {PRODUCT.singular}</Button>}
            />
          ))}
      </PageContent>
      {/* Outside the content region: a modal is not part of the page's scroll.
          Mounted only while it is open, so each opening starts on a blank form
          rather than on whatever the last one was abandoned holding. */}
      {registering && <NewProductDialog open onClose={() => setRegistering(false)} />}
      {creating && product && (
        <NewInitiativeDialog
          product={product}
          open
          onClose={() => setCreating(false)}
          // Started from a capability: it is the home the user already picked.
          defaultCapabilityId={capability?.id}
        />
      )}
    </div>
  )
}
