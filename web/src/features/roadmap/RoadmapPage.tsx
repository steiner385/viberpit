import { useMemo, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useProject, useRoadmapMap, useSyncProject, useTriage } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { EmptyState } from '@/ui/EmptyState'
import { Banner } from '@/ui/Banner'
import { Button } from '@/ui/Button'
import { Breadcrumb, type BreadcrumbItem } from '@/ui/Breadcrumb'
import { useItemOrdering } from '@/session/useSessionOrdering'
import { INITIATIVE, PRODUCT, ROADMAP } from '@/features/products/vocab'
import {
  capabilityPath, inboxPath, inboxProductForSearch, productPath, roadmapOpenItem, roadmapPath,
  roadmapScopeForSearch,
} from './roadmapNavState'
import { capabilityTrail, findCapability, findProduct, flattenCapabilities } from './lookup'
import { PortfolioDetail } from './detail/PortfolioDetail'
import { ProductDetail } from './detail/ProductDetail'
import { CapabilityDetail } from './detail/CapabilityDetail'
import { NewInitiativeDialog } from './detail/NewInitiativeDialog'
import { NewProductDialog } from './detail/NewProductDialog'
import { INBOX_ORDERING_SPEC } from './inbox/inboxOrdering'
import { InboxWelcome } from './inbox/InboxWelcome'
import { ProposalDetail } from './inbox/ProposalDetail'

/** `/roadmap`, `/roadmap/:productSlug`, `/roadmap/:productSlug/capabilities/:capSlug` —
 *  one routed page, three detail levels chosen by the params (spec §7), plus the
 *  Inbox scope (`?scope=inbox`), whose detail level is one proposal (§12). */
export function RoadmapPage() {
  const { search } = useLocation()
  const navigate = useNavigate()
  const item = roadmapOpenItem(useParams(), new URLSearchParams(search))
  const inbox = roadmapScopeForSearch(search) === 'inbox'
  // An initiative is never open on THESE routes (it has its own page), and a
  // proposal is the Inbox branch's concern, so the rest of the component
  // reasons about the product/capability item alone. In the Inbox scope
  // NOTHING product-level is open, even on `/roadmap/kindash?scope=inbox`: one
  // page shows one body, and the scope is what says which.
  const opened = !inbox && item && item.kind !== 'initiative' && item.kind !== 'proposal' ? item : null
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

  // ---- Inbox scope (§12) -------------------------------------------------
  // `poll: false` for the same reason the map fetch above has it: the list
  // column observes this exact cache entry (same `?product=` scope) and owns
  // the 15 s interval. Two polling observers of one key are two requests.
  const inboxProduct = inboxProductForSearch(search)
  const triage = useTriage(inboxProduct ?? undefined, { poll: false })
  const proposalId = item?.kind === 'proposal' ? item.proposalId : null
  // `e` in the list column opens the row WITH the score fields focused, which
  // is the whole of what "edit" means now that the editor is the pane.
  const editing = new URLSearchParams(search).get('edit') === '1'
  const proposal = proposalId ? triage.data?.find((p) => p.id === proposalId) ?? null : null
  // The proposal's OWN product, not the one the `?product=` chip names: an
  // unscoped inbox mixes products, and offering a KinDash capability as the
  // home for a ViberPit issue would accept it onto the wrong tree.
  const proposalProduct = proposal?.projectSlug ? findProduct(map, proposal.projectSlug) : null
  const capabilities = useMemo(
    () => flattenCapabilities(proposalProduct?.capabilities ?? [])
      .map(({ node, depth }) => ({ id: node.id, slug: node.slug, title: node.title, depth })),
    [proposalProduct],
  )
  // "Next" means next ON SCREEN, so it is computed through the SAME ordering
  // hook and the SAME persisted prefs (`roadmap-inbox`) the list column orders
  // its rows with — the server hands the queue over in `created_at DESC`, which
  // is not what the user is looking at once anything is grouped or sorted. The
  // column's search box is excluded on purpose: it narrows the view, not the
  // queue, and advancing should not dead-end on text typed in the other pane.
  const inboxOrder = useItemOrdering(triage.data ?? [], INBOX_ORDERING_SPEC).orderedSessions
  // Decided: the row is leaving the queue, so the pane cannot stay on it. The
  // next one in list order (or the one before it, when this was the last) keeps
  // the queue moving without a trip back to the list; `replace`, because the
  // address just left behind names a proposal that is no longer pending.
  const onDecided = () => {
    const list = inboxOrder
    const i = list.findIndex((p) => p.id === proposalId)
    const next = i === -1 ? undefined : (list[i + 1] ?? list[i - 1])
    navigate(
      next ? inboxPath({ product: inboxProduct, proposal: next.id }) : inboxPath({ product: inboxProduct }),
      { replace: true },
    )
  }
  // A proposal the queue does not contain: decided in another tab, decided by
  // somebody else, or a stale bookmark. Gated on the queue having ARRIVED — a
  // failed fetch also leaves `proposal` null, and that is a broken connection.
  const proposalMissing = inbox && proposalId !== null && triage.data !== undefined && !proposal
  /** First load of the queue: no answer yet either way. */
  const inboxPending = triage.isLoading && !triage.data

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
  if (inbox) {
    // The Inbox is a scope, not a thing on the map: it is always "the Inbox",
    // whether one proposal is open or none, and the `?product=` narrowing is a
    // crumb rather than a title so the section the user is in never moves.
    title = ROADMAP.inbox
    crumbs.push({ label: ROADMAP.inbox, to: inboxProduct ? inboxPath({}) : undefined })
    if (inboxProduct) crumbs.push({ label: inboxProduct })
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
  const mapActions = opened?.kind === 'product' && product
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
  // None of them belong to the Inbox: its actions are the decision buttons on
  // the proposal itself, and a `+ Product` beside them would be noise between
  // the user and an irreversible accept.
  const productActions = inbox ? undefined : mapActions

  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="standard">
      <PageHeader
        title={title}
        meta={crumbs.length > 1 ? <Breadcrumb items={crumbs} /> : undefined}
        actions={productActions}
      />
      <PageContent busy={inbox ? inboxPending : isLoading && !map}>
        {/* The Inbox scope is a page of its own inside this one: a different
            query, a different detail level, and none of the map's states. */}
        {inbox && (
          <>
            {/* Same copy as the list column, which fails from the same query. */}
            {triage.isError && <Banner tone="danger" className="mb-4" title="Could not load the inbox." />}
            {proposal && (
              <ProposalDetail
                proposal={proposal}
                capabilities={capabilities}
                scopeProduct={inboxProduct}
                autoFocusScores={editing}
                onDone={onDecided}
              />
            )}
            {proposalMissing && (
              <EmptyState
                title="Proposal not found — it may have been decided"
                description="It is no longer in the queue. Somebody (or another tab) may have accepted or rejected it."
                action={(
                  <Link className="text-accent hover:underline" to={inboxPath({ product: inboxProduct })}>
                    Back to the {ROADMAP.inbox.toLowerCase()}
                  </Link>
                )}
              />
            )}
            {/* Nothing open, and not still waiting for the queue: the pane says
                what is waiting rather than sitting blank. A failed fetch is not
                inbox zero, so it says so instead. */}
            {!proposal && !proposalMissing && !inboxPending && (
              triage.isError && !triage.data
                ? (
                  <EmptyState
                    title={`No ${ROADMAP.inbox.toLowerCase()} to show`}
                    description="It will fill in as soon as the queue loads again."
                  />
                )
                : <InboxWelcome count={triage.data?.length ?? 0} product={inboxProduct} />
            )}
          </>
        )}
        {/* Errors are a Banner, first child of the content region. Same copy as the
            list column, which fails from the same query. */}
        {!inbox && isError && <Banner tone="danger" className="mb-4" title="Could not load the roadmap." />}
        {/* Sync opts out of the global error toast (`PM_MUTATION_META`), so
            without this a refused sync said nothing at all — the button just
            stopped spinning. Same idiom and position as InitiativePage's
            failed Advance. */}
        {!inbox && sync.isError && (
          <Banner
            tone="danger"
            className="mb-4"
            title={extractMutationErrorMessage(sync.error, 'Could not request sync')}
          />
        )}
        {/* Nothing open: the portfolio IS the page, not a "pick something" prompt. */}
        {map && !opened && !inbox && <PortfolioDetail map={map} />}
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
        {!inbox && !map && !isLoading && (isError
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
