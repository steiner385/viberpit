import { useState } from 'react'
import { useRetireCapability, useMergeCapabilities } from '@/api/hooks'
import type { CapabilityNode, ProductNode } from '@/api/roadmap'
import { flattenCapabilities, findCapability } from '@/features/roadmap/lookup'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Button } from '@/ui/Button'
import { Select } from '@/ui/Select'
import { CAPABILITY, capabilityStateLabel } from './vocab'
import { RemapDependentsDialog } from './RemapDependentsDialog'

interface Props {
  product: ProductNode
  capabilities: CapabilityNode[]
}

/** The capability and everything under it — the parents it may never be given,
 *  because each would close a cycle the server refuses with `parent_cycle`.
 *  Read from the PRODUCT's copy of the node: the prop may be a narrowed view
 *  that carries no children. */
function selfAndDescendants(product: ProductNode, capability: CapabilityNode): Set<string> {
  const node = findCapability(product, capability.slug) ?? capability
  const walk = (n: CapabilityNode): string[] => [n.slug, ...n.children.flatMap(walk)]
  return new Set(walk(node))
}

/** Products → <product> → Capabilities (capability-management spec, PR 3):
 *  list every capability with its state and open-issue count, retire one
 *  (server guards on live dependents with a 422 `has_dependents`), or merge
 *  one into another (Task 10) and re-map its dependents (Task 11).
 *
 *  The map payload's `CapabilityCounts` has no trustworthy "N initiatives
 *  touch this" total — `CapabilityNode.initiatives` explicitly excludes
 *  primary-mapped initiatives — so this table does not attempt to preview a
 *  precise dependents count. The retire guard is fully enforced server-side;
 *  its refusal message (surfaced below via `role="alert"`) is the
 *  authoritative signal to the user, not anything this table displays. */
export function CapabilitiesTab({ product, capabilities }: Props) {
  const rows = flattenCapabilities(capabilities)
  const retire = useRetireCapability(product.slug)
  const merge = useMergeCapabilities(product.slug)
  const [confirming, setConfirming] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [merging, setMerging] = useState<string | null>(null)
  const [mergeTarget, setMergeTarget] = useState<string>('')
  const [mergeError, setMergeError] = useState<string | null>(null)
  const [mergeInfo, setMergeInfo] = useState<string | null>(null)
  const [remapping, setRemapping] = useState<string | null>(null)
  const remapNode = remapping ? rows.find((r) => r.node.slug === remapping)?.node : undefined

  const doRetire = async (slug: string) => {
    setError(null)
    try {
      await retire.mutateAsync({ slug })
      setConfirming(null)
    } catch (e) {
      setError(extractMutationErrorMessage(e, `Could not retire ${slug}`))
    }
  }

  const doMerge = async (fromSlug: string, intoSlug: string) => {
    setMergeError(null)
    setMergeInfo(null)
    try {
      const result = await merge.mutateAsync({ fromSlug, intoSlug })
      setMerging(null)
      setMergeTarget('')
      if (result.linksNotRepointed > 0) {
        setMergeInfo(
          `Merge complete — ${result.linksNotRepointed} cross-product link(s) could not be repointed and still point at the retired capability.`,
        )
      }
    } catch (e) {
      setMergeError(extractMutationErrorMessage(e, `Could not merge ${fromSlug}`))
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {error && <div role="alert" className="text-sm text-danger">{error}</div>}
      {mergeError && <div role="alert" className="text-sm text-danger">{mergeError}</div>}
      {mergeInfo && <div className="text-sm text-ink">{mergeInfo}</div>}
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs uppercase text-ink-muted">
            <th className="py-1">{CAPABILITY.singular}</th>
            <th className="py-1">State</th>
            <th className="py-1">Open issues</th>
            <th className="py-1" />
          </tr>
        </thead>
        <tbody>
          {rows.map(({ node, depth }) => {
            const blocked = selfAndDescendants(product, node)
            // An already-sunset capability is not a place to fold live work
            // INTO — offering it would let a merge move issues and initiatives
            // straight onto a retired capability, which is exactly what
            // `retire()`'s own dependents guard exists to prevent.
            const mergeTargets = rows
              .filter(({ node: n }) => !blocked.has(n.slug) && n.state !== 'sunset')
              .map(({ node: n }) => n)
            // …and there is nothing to retire about a capability already there.
            const retirable = node.state !== 'sunset'

            return (
              <tr key={node.id} className="border-t border-line">
                <td className="py-2" style={{ paddingLeft: depth * 16 }}>
                  {node.title ?? node.slug}
                </td>
                <td className="py-2">{capabilityStateLabel(node.state)}</td>
                <td className="py-2">{node.counts.openIssues}</td>
                <td className="py-2 text-right">
                  {confirming === node.slug ? (
                    <span className="flex items-center justify-end gap-2">
                      {/* Name the side effect: retiring does not just sunset
                          this row, it silently moves every live child up to
                          this capability's own parent. */}
                      <span className="text-xs text-ink-muted">
                        Retire {node.title ?? node.slug}? Any sub-{CAPABILITY.plural.toLowerCase()} will
                        move up to its parent.
                      </span>
                      <Button size="sm" variant="danger" onClick={() => void doRetire(node.slug)}
                              disabled={retire.isPending}>
                        Confirm
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setConfirming(null)}>
                        Cancel
                      </Button>
                    </span>
                  ) : merging === node.slug ? (
                    <span className="flex flex-col items-end justify-end gap-2">
                      <span className="text-xs text-ink-muted">Merge {node.title ?? node.slug} into:</span>
                      <Select
                        role="combobox"
                        aria-label={`Merge ${node.title ?? node.slug} into`}
                        size="sm"
                        value={mergeTarget}
                        onChange={(e) => setMergeTarget(e.target.value)}
                      >
                        <option value="">Select a capability</option>
                        {mergeTargets.map((target) => (
                          <option key={target.id} value={target.slug}>
                            {target.title ?? target.slug}
                          </option>
                        ))}
                      </Select>
                      <span className="flex items-center justify-end gap-2">
                        <Button
                          size="sm"
                          variant="danger"
                          onClick={() => void doMerge(node.slug, mergeTarget)}
                          disabled={merge.isPending || !mergeTarget}
                        >
                          Confirm merge
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => {
                            setMerging(null)
                            setMergeTarget('')
                          }}
                        >
                          Cancel
                        </Button>
                      </span>
                    </span>
                  ) : (
                    <span className="flex items-center justify-end gap-2">
                      <Button size="sm" variant="ghost" onClick={() => setRemapping(node.slug)}>
                        Re-map…
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setMerging(node.slug)}>
                        Merge into…
                      </Button>
                      {retirable && (
                        <Button size="sm" variant="ghost" onClick={() => setConfirming(node.slug)}>
                          Retire
                        </Button>
                      )}
                    </span>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {/* Mounted only while open (the RoadmapPage idiom — see
          `CapabilityDetail.tsx`): a left-mounted dialog would keep polling
          `useLeaves` for a capability the user is no longer looking at. */}
      {remapNode && (
        <RemapDependentsDialog
          product={product}
          capability={remapNode}
          allCapabilities={rows.map((r) => r.node)}
          open
          onClose={() => setRemapping(null)}
        />
      )}
    </div>
  )
}
