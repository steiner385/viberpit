// A capability's links to other capabilities (spec §8 "Capability links — chip
// editor", R8c-2), mounted on the capability's Dependencies tab
// (docs/PAGE-TABS.md §5): a labelled section saying what a link IS, the
// chips it has with a remove on each, and an add row that stays behind a
// "+ Link" button until asked for — almost no capability has links, so a
// permanently open three-select form read as an unexplained chore. Every
// change sends the FULL list as one `links` op — the server
// replaces the doc's link block rather than merging into it, so a partial
// list would silently drop the rest.
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { X } from 'lucide-react'
import type { CapabilityLink, CapabilityNode, ProductNode, RoadmapMap } from '@/api/roadmap'
import type { MapChangeLink } from '@/api/pm'
import { useEditMapChanges, useRoadmapMap, mapChangesErrorMessage } from '@/api/hooks'
import { Select } from '@/ui/Select'
import { Button } from '@/ui/Button'
import { IconButton } from '@/ui/IconButton'
import { SectionHeading } from '@/ui/SectionHeading'
import { CAPABILITY, PRODUCT } from '@/features/products/vocab'
import { findCapability, findProduct, flattenCapabilities } from '../lookup'
import { capabilityPath } from '../roadmapNavState'

/** The two kinds a feature doc may declare (`FEATURE_LINK_KINDS`). `chip` is the
 *  predicate a chip reads with ("powered by · X"); `sentence` completes the add
 *  row's sentence, whose subject is the capability itself ("Tasks is powered
 *  by …"). */
const LINK_KIND: Record<string, { chip: string; sentence: string }> = {
  powered_by: { chip: 'powered by', sentence: 'is powered by' },
  integrates_with: { chip: 'integrates with', sentence: 'integrates with' },
}
const kindChip = (kind: string) => LINK_KIND[kind]?.chip ?? kind.replace(/_/g, ' ')

/** "Sessions (ViberPit)" when the target is on the map; the raw slug pair when
 *  it is not (a link to a capability the index has not seen yet). */
const targetOf = (map: RoadmapMap | undefined, l: CapabilityLink) => {
  const product = findProduct(map, l.projectSlug)
  const node = product ? findCapability(product, l.featureSlug) : null
  return node ? `${node.title ?? node.slug} (${product?.name ?? l.projectSlug})` : `${l.projectSlug}/${l.featureSlug}`
}
/** The map names a link's target `projectSlug`/`featureSlug`; the API wants
 *  `project`/`feature`. The conversion lives here, at the one boundary. */
const toApi = (l: CapabilityLink): MapChangeLink => ({
  kind: l.kind as MapChangeLink['kind'], project: l.projectSlug, feature: l.featureSlug,
})
const keyOf = (l: CapabilityLink) => `${l.kind}:${l.projectSlug}/${l.featureSlug}`
/** Two link lists holding the same set — order is the doc's, not a difference. */
const sameLinks = (a: CapabilityLink[], b: CapabilityLink[]) => {
  if (a.length !== b.length) return false
  const have = new Set(a.map(keyOf))
  return b.every((l) => have.has(keyOf(l)))
}

export function CapabilityLinksEditor({ product, capability }: {
  product: ProductNode
  capability: CapabilityNode
}) {
  // The whole map, because a link may point at any product's capability; the
  // page already holds this query, so the editor costs no extra fetch.
  const { data: map } = useRoadmapMap({ poll: false })
  const edit = useEditMapChanges(product.slug)
  const [adding, setAdding] = useState(false)
  const [kind, setKind] = useState<string>('powered_by')
  const [targetProduct, setTargetProduct] = useState('')
  const [targetCapability, setTargetCapability] = useState('')
  const [failure, setFailure] = useState<string | null>(null)
  // The list the LAST successful send put in the doc. `capability.links` comes
  // from the indexed map, which only catches up on the next pm-sync pass — so
  // between the commit and that pass the chips are stale, and a second edge
  // computed from them would re-send the state before the first one (remove X,
  // then remove Y from stale chips, sends `[X, Z]` and X comes back). Every send
  // and the chips themselves are based on this while it is set.
  // Keyed to the capability it was sent FOR: the editor does not unmount when
  // the page swaps capability, and a send is a real GitHub round trip, so a
  // swap while one is in flight would otherwise let the late `setSent` land
  // one capability's links on another (and the next edit would write them
  // into the wrong doc). A record for another id is simply ignored.
  const [sent, setSent] = useState<{ id: string; links: CapabilityLink[] } | null>(null)

  let base = sent?.id === capability.id ? sent.links : null
  if (base && sameLinks(base, capability.links)) {
    // The map caught up — the prop is the truth again, and holding the copy
    // would freeze out a change made from somewhere else.
    setSent(null)
    base = null
  }

  const links = base ?? capability.links
  const taken = new Set(links.map(keyOf))
  const products = map?.products ?? []
  const chosen = products.find((p) => p.slug === targetProduct)
  // That product's tree, minus this capability itself (a self-link) and minus
  // the targets it already links with this kind (a duplicate in the doc).
  const targets = chosen
    ? flattenCapabilities(chosen.capabilities).filter(({ node }) =>
      !(chosen.slug === product.slug && node.slug === capability.slug)
      && !taken.has(`${kind}:${chosen.slug}/${node.slug}`))
    : []
  const canAdd = targetProduct !== '' && targetCapability !== '' && !edit.isPending
  const title = capability.title ?? capability.slug
  const lowerCapability = CAPABILITY.singular.toLowerCase()

  const closeAddRow = () => {
    setAdding(false)
    setTargetProduct('')
    setTargetCapability('')
  }

  const send = async (next: CapabilityLink[], after?: () => void) => {
    // Captured before the await: the record must name the capability this
    // send was for, not whichever one is on screen when it resolves.
    const forId = capability.id
    setFailure(null)
    try {
      await edit.mutateAsync({ op: 'links', slug: capability.slug, links: next.map(toApi) })
    } catch (e) {
      setFailure(mapChangesErrorMessage(e, 'Could not save the links.'))
      return
    }
    setSent({ id: forId, links: next })
    after?.()
  }

  return (
    <section aria-labelledby="capability-links-heading" className="flex flex-col gap-2">
      <div>
        <SectionHeading id="capability-links-heading">Links</SectionHeading>
        <p className="text-xs text-ink-muted">
          What this {lowerCapability} is powered by or integrates with — other {CAPABILITY.plural.toLowerCase()},
          here or in another {PRODUCT.singular.toLowerCase()} — as declared in its feature doc.
        </p>
      </div>
      {links.length === 0
        ? <p className="text-sm text-ink-muted">No links yet.</p>
        : (
          <ul aria-label={`Linked ${CAPABILITY.plural.toLowerCase()}`} className="flex flex-wrap gap-2">
            {links.map((l) => {
              const label = `${kindChip(l.kind)} · ${targetOf(map, l)}`
              return (
                <li key={keyOf(l)} className="flex items-center gap-1 rounded-pill border border-edge bg-surface-raised py-0.5 pl-3 pr-1 text-xs text-ink">
                  <Link to={capabilityPath(l.projectSlug, l.featureSlug)} className="hover:underline">{label}</Link>
                  <IconButton
                    size="sm"
                    aria-label={`Remove link: ${label}`}
                    disabled={edit.isPending}
                    onClick={() => void send(links.filter((x) => keyOf(x) !== keyOf(l)))}
                  >
                    <X size={14} aria-hidden="true" />
                  </IconButton>
                </li>
              )
            })}
          </ul>
        )}
      {adding
        ? (
          <form
            aria-label="Add a link"
            className="flex flex-col gap-1"
            onSubmit={(e) => {
              e.preventDefault()
              if (!canAdd) return
              const added: CapabilityLink = { kind, projectSlug: targetProduct, featureSlug: targetCapability }
              void send([...links, added], closeAddRow)
            }}
          >
            {/* One sentence, subject first: "Tasks · is powered by · ViberPit / Sessions". */}
            <div className="flex flex-wrap items-center gap-2 text-sm text-ink">
              <span className="font-medium">{title}</span>
              <Select size="sm" aria-label="Relationship" value={kind} disabled={edit.isPending} onChange={(e) => { setKind(e.target.value); setTargetCapability('') }}>
                {Object.entries(LINK_KIND).map(([k, { sentence }]) => <option key={k} value={k}>{sentence}</option>)}
              </Select>
              <Select
                size="sm"
                aria-label={PRODUCT.singular}
                value={targetProduct}
                disabled={edit.isPending}
                onChange={(e) => { setTargetProduct(e.target.value); setTargetCapability('') }}
              >
                <option value="">— {PRODUCT.singular.toLowerCase()} —</option>
                {products.map((p) => <option key={p.slug} value={p.slug}>{p.name ?? p.slug}</option>)}
              </Select>
              <span aria-hidden="true" className="text-ink-muted">/</span>
              <Select
                size="sm"
                aria-label={CAPABILITY.singular}
                value={targetCapability}
                disabled={edit.isPending || targetProduct === ''}
                onChange={(e) => setTargetCapability(e.target.value)}
              >
                <option value="">— {lowerCapability} —</option>
                {targets.map(({ node, depth }) => (
                  <option key={node.slug} value={node.slug}>{'  '.repeat(depth)}{node.title ?? node.slug}</option>
                ))}
              </Select>
              <Button type="submit" size="sm" disabled={!canAdd} loading={edit.isPending}>Add</Button>
              <Button type="button" size="sm" variant="ghost" disabled={edit.isPending} onClick={closeAddRow}>Cancel</Button>
            </div>
            {/* Add is not a local save: it commits to the product's map-changes
                branch and opens (or appends to) its PR, which is a surprise
                unless said here. */}
            <p className="text-xs text-ink-muted">
              Add proposes the link in {product.name ?? product.slug}&rsquo;s map-changes PR; it shows as proposed until that PR merges.
            </p>
          </form>
        )
        : (
          <div>
            <Button size="sm" variant="ghost" disabled={edit.isPending} onClick={() => setAdding(true)}>+ Link</Button>
          </div>
        )}
      {failure && <p role="alert" className="text-sm text-danger">{failure}</p>}
    </section>
  )
}
