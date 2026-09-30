import { useState } from 'react'
import { useRetireCapability, useMergeCapabilities } from '@/api/hooks'
import type { CapabilityNode, ProductNode } from '@/api/roadmap'
import type { Consequence } from '@/api/sdlc'
import { flattenCapabilities, findCapability } from '@/features/roadmap/lookup'
import { ConsequenceList } from '@/features/roadmap/guidance/ConsequenceList'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Button } from '@/ui/Button'
import { Dialog } from '@/ui/Dialog'
import { Select } from '@/ui/Select'
import { CAPABILITY, ISSUE, capabilityStateLabel } from './vocab'
import { RemapDependentsDialog } from './RemapDependentsDialog'

const titleOf = (n: CapabilityNode) => n.title ?? n.slug
const count = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

/** What retiring *capability* does, in the shape every other gate renders
 *  through (`ConsequenceList`). Built here rather than served: the map payload
 *  already carries every fact the sentence needs (children, parent, the open
 *  issue count), and `retire()` itself has no explain seam. Mirrors
 *  `server/pm/map_changes.py:retire` step for step — the doc commit, the child
 *  re-parent, the dependents guard, the cleared triage suggestions — so the
 *  dialog names what the row's old 12px sentence left out (2026-09-30 Roadmap
 *  critique, P2: "no mention of the 2 open issues shown two cells left"). */
function retireConsequence(product: ProductNode, capability: CapabilityNode): Consequence {
  // The PRODUCT's copy: the prop may be a narrowed view with no children.
  const node = findCapability(product, capability.slug) ?? capability
  const children = node.children.filter((c) => c.state !== 'sunset')
  const parent = node.parentSlug ? findCapability(product, node.parentSlug) : null
  const issues = node.counts.openIssues
  const write = (text: string): Consequence['immediate'][number] => ({ text, spawnsSession: false, writesGithub: true })
  const note = (text: string): Consequence['immediate'][number] => ({ text, spawnsSession: false, writesGithub: false })
  return {
    available: true,
    unavailableReason: null,
    immediate: [
      write(`${titleOf(node)} is marked ${capabilityStateLabel('sunset')} in its feature doc and leaves the tree`),
      ...(children.length > 0 ? [write(
        `${count(children.length, `sub-${CAPABILITY.singular.toLowerCase()}`, `sub-${CAPABILITY.plural.toLowerCase()}`)}`
        + ` (${children.map(titleOf).join(', ')}) ${children.length === 1 ? 'moves' : 'move'} up to `
        + (parent ? titleOf(parent) : 'the top level'),
      )] : []),
      ...(issues > 0 ? [note(
        `${count(issues, `open ${ISSUE.singular.toLowerCase()}`, `open ${ISSUE.plural.toLowerCase()}`)} counted under it`
        + ' stay where they are — if any is mapped directly to it, the retire is refused and nothing changes;'
        + ' re-map them or merge into another capability first',
      )] : []),
      note('Inbox proposals that suggested this capability lose that suggestion and come back as "no fit"'),
    ],
    then: [],
    when: 'now',
    reversal: 'Not from here — the state is written into the feature doc; restoring it means editing that doc.',
    gate: null,
  }
}

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
  const confirmingNode = confirming ? rows.find((r) => r.node.slug === confirming)?.node : undefined

  // The refusal (422 `has_dependents`) is shown inside the dialog, against the
  // tap it answers; closing the dialog drops it with the question.
  const cancelRetire = () => { setConfirming(null); setError(null) }
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
                  {merging === node.slug ? (
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
      {/* A destructive gate with consequences is a Dialog, like every other one
          in the roadmap (won't-do, reject, unblock) — not a 12px sentence and a
          `sm` Confirm inline in a table cell, which named neither the open
          issues two cells to the left nor which children move where. */}
      {confirmingNode && (
        <Dialog
          open
          size="sm"
          title={`Retire ${titleOf(confirmingNode)}?`}
          description="Retiring is a map change: it is committed to the product's map-changes branch and applied at once."
          onClose={cancelRetire}
        >
          <div className="flex flex-col gap-3">
            <ConsequenceList consequence={retireConsequence(product, confirmingNode)} />
            {error && <div role="alert" className="text-sm text-danger">{error}</div>}
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={cancelRetire} disabled={retire.isPending}>
                Cancel
              </Button>
              <Button variant="danger" onClick={() => void doRetire(confirmingNode.slug)} loading={retire.isPending}>
                Retire {titleOf(confirmingNode)}
              </Button>
            </div>
          </div>
        </Dialog>
      )}
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
