import { useState } from 'react'
import { useRetireCapability } from '@/api/hooks'
import type { CapabilityNode, ProductNode } from '@/api/roadmap'
import { flattenCapabilities } from '@/features/roadmap/lookup'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Button } from '@/ui/Button'
import { CAPABILITY, capabilityStateLabel } from './vocab'

interface Props {
  product: ProductNode
  capabilities: CapabilityNode[]
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
  const [confirming, setConfirming] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const doRetire = async (slug: string) => {
    setError(null)
    try {
      await retire.mutateAsync({ slug })
      setConfirming(null)
    } catch (e) {
      setError(extractMutationErrorMessage(e, `Could not retire ${slug}`))
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {error && <div role="alert" className="text-sm text-danger">{error}</div>}
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
          {rows.map(({ node, depth }) => (
            <tr key={node.id} className="border-t border-line">
              <td className="py-2" style={{ paddingLeft: depth * 16 }}>
                {node.title ?? node.slug}
              </td>
              <td className="py-2">{capabilityStateLabel(node.state)}</td>
              <td className="py-2">{node.counts.openIssues}</td>
              <td className="py-2 text-right">
                {confirming === node.slug ? (
                  <span className="flex items-center justify-end gap-2">
                    <span className="text-xs text-ink-muted">Retire {node.title ?? node.slug}?</span>
                    <Button size="sm" variant="danger" onClick={() => void doRetire(node.slug)}
                            disabled={retire.isPending}>
                      Confirm
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setConfirming(null)}>
                      Cancel
                    </Button>
                  </span>
                ) : (
                  <Button size="sm" variant="ghost" onClick={() => setConfirming(node.slug)}>
                    Retire
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
