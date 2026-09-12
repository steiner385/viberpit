// A capability's links to other capabilities (spec §8 "Capability links — chip
// editor on Overview", R8c-2): the chips it has, a remove on each, and one row
// to add another. Every change sends the FULL list as one `links` op — the
// server replaces the doc's link block rather than merging into it, so a
// partial list would silently drop the rest.
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { X } from 'lucide-react'
import type { CapabilityLink, CapabilityNode, ProductNode } from '@/api/roadmap'
import type { MapChangeLink } from '@/api/pm'
import { useEditMapChanges, useRoadmapMap, mapChangesErrorMessage } from '@/api/hooks'
import { Field } from '@/ui/Field'
import { Select } from '@/ui/Select'
import { Button } from '@/ui/Button'
import { IconButton } from '@/ui/IconButton'
import { CAPABILITY, PRODUCT } from '@/features/products/vocab'
import { flattenCapabilities } from '../lookup'
import { capabilityPath } from '../roadmapNavState'

/** The two kinds a feature doc may declare (`FEATURE_LINK_KINDS`), in the words
 *  the chips and the add row both read them with. */
const LINK_KIND_LABEL: Record<string, string> = {
  powered_by: 'powered by',
  integrates_with: 'integrates with',
}

const labelOf = (l: CapabilityLink) =>
  `${LINK_KIND_LABEL[l.kind] ?? l.kind.replace(/_/g, ' ')} · ${l.projectSlug}/${l.featureSlug}`
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
  const [sent, setSent] = useState<CapabilityLink[] | null>(null)
  // Render-phase reset (the `ProposalDetail` "seededFor" idiom, React's
  // documented "resetting state when a prop changes"): the editor does not
  // unmount when the page swaps capability, and one capability's sent list is
  // not another's.
  const [seededFor, setSeededFor] = useState(capability.id)

  let base = sent
  if (seededFor !== capability.id) {
    setSeededFor(capability.id)
    setSent(null)
    base = null
  } else if (sent && sameLinks(sent, capability.links)) {
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

  const send = async (next: CapabilityLink[], after?: () => void) => {
    setFailure(null)
    try {
      await edit.mutateAsync({ op: 'links', slug: capability.slug, links: next.map(toApi) })
    } catch (e) {
      setFailure(mapChangesErrorMessage(e, 'Could not save the links.'))
      return
    }
    setSent(next)
    after?.()
  }

  return (
    <div className="flex flex-col gap-3">
      {links.length === 0
        ? <p className="text-sm text-ink-muted">No linked {CAPABILITY.plural.toLowerCase()} yet.</p>
        : (
          <ul aria-label={`Linked ${CAPABILITY.plural.toLowerCase()}`} className="flex flex-wrap gap-2">
            {links.map((l) => {
              const label = labelOf(l)
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
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (!canAdd) return
          const added: CapabilityLink = { kind, projectSlug: targetProduct, featureSlug: targetCapability }
          void send([...links, added], () => { setTargetCapability('') })
        }}
      >
        <Field label="Relationship" htmlFor="link-kind">
          <Select id="link-kind" value={kind} disabled={edit.isPending} onChange={(e) => { setKind(e.target.value); setTargetCapability('') }}>
            {Object.entries(LINK_KIND_LABEL).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
          </Select>
        </Field>
        <Field label={PRODUCT.singular} htmlFor="link-product">
          <Select
            id="link-product"
            value={targetProduct}
            disabled={edit.isPending}
            onChange={(e) => { setTargetProduct(e.target.value); setTargetCapability('') }}
          >
            <option value="">— choose —</option>
            {products.map((p) => <option key={p.slug} value={p.slug}>{p.name ?? p.slug}</option>)}
          </Select>
        </Field>
        <Field label={CAPABILITY.singular} htmlFor="link-capability">
          <Select
            id="link-capability"
            value={targetCapability}
            disabled={edit.isPending || targetProduct === ''}
            onChange={(e) => setTargetCapability(e.target.value)}
          >
            <option value="">— choose —</option>
            {targets.map(({ node, depth }) => (
              <option key={node.slug} value={node.slug}>{'  '.repeat(depth)}{node.title ?? node.slug}</option>
            ))}
          </Select>
        </Field>
        <Button type="submit" disabled={!canAdd} loading={edit.isPending}>Add</Button>
      </form>
      {failure && <p role="alert" className="text-sm text-danger">{failure}</p>}
    </div>
  )
}
