// Re-map one or more initiatives/issues off a capability onto another, in one
// action (capability-management spec, Task 11). Per the design decision, no
// new bulk endpoint — this fires the existing single-item `useUpdateInitiative`
// / `useUpdateOverlay` PATCH mutations once per selected item.
import { useState } from 'react'
import { useLeaves, useUpdateInitiative, useUpdateOverlay } from '@/api/hooks'
import type { CapabilityNode, ProductNode } from '@/api/roadmap'
import type { UpdateInitiativeInput } from '@/api/sdlc'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Select } from '@/ui/Select'
import { Button } from '@/ui/Button'

interface Props {
  /** Supplies `initiatives`: every initiative of the product, flat — filtered
   *  below to the ones that touch `capability` (primary or otherwise). */
  product: ProductNode
  capability: CapabilityNode
  allCapabilities: CapabilityNode[]
  open: boolean
  onClose: () => void
}

function toggle(set: Set<string>, setter: (s: Set<string>) => void, key: string) {
  const next = new Set(set)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  setter(next)
}

/** Mounted only while open (the RoadmapPage idiom — see `CapabilityDetail.tsx`),
 *  so the `useLeaves` fetch below is never wasted on a closed dialog. */
export function RemapDependentsDialog({ product, capability, allCapabilities, open, onClose }: Props) {
  const initiatives = product.initiatives.filter(
    (i) => i.primaryCapability === capability.slug || i.capabilities.includes(capability.slug),
  )
  // PRs aren't classified onto capabilities the same way issues are — only
  // issues are offered as re-mappable rows here.
  const { data: leaves } = useLeaves({ capability: capability.slug })
  const issues = (leaves ?? []).filter((leaf) => leaf.kind === 'issue')

  const [selectedInitiatives, setSelectedInitiatives] = useState<Set<string>>(new Set())
  const [selectedIssues, setSelectedIssues] = useState<Set<string>>(new Set())
  const [target, setTarget] = useState('')
  const [error, setError] = useState<string | null>(null)
  const updateInitiative = useUpdateInitiative()
  const updateOverlay = useUpdateOverlay()
  const moving = updateInitiative.isPending || updateOverlay.isPending

  // `capabilityIds` REPLACES an initiative's full touched-capability set
  // (UpdateInitiativeInput's own doc comment). An initiative that touches
  // BOTH `capability` and some unrelated capability must keep that other
  // touch — the user only asked to re-map ITS relationship to `capability`,
  // not to blow away everything else it's linked to. `primaryCapabilityId`
  // is sent only when `capability` was actually this initiative's primary —
  // "send only what changed" (same doc comment) means an initiative whose
  // primary lives elsewhere gets no primary change at all.
  const moveSelected = async () => {
    setError(null)
    try {
      await Promise.all([
        ...[...selectedInitiatives].map((slug) => {
          const initiative = initiatives.find((i) => i.slug === slug)
          const otherTouches = (initiative?.capabilities ?? []).filter((s) => s !== capability.slug)
          const input: UpdateInitiativeInput = {
            capabilityIds: Array.from(new Set([...otherTouches, target])),
          }
          if (initiative?.primaryCapability === capability.slug) input.primaryCapabilityId = target
          return updateInitiative.mutateAsync({ slug, input })
        }),
        ...[...selectedIssues].map((id) => updateOverlay.mutateAsync({
          itemId: id, edit: { featureId: target },
        })),
      ])
      onClose()
    } catch (e) {
      setError(extractMutationErrorMessage(e, 'Could not move every selected item'))
    }
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={`Re-map dependents of ${capability.title ?? capability.slug}`}
      size="md"
    >
      <div className="flex flex-col gap-3">
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <fieldset className="flex flex-col gap-1">
          <legend className="text-xs uppercase text-ink-muted">Initiatives</legend>
          {initiatives.length === 0 && <p className="text-xs text-ink-muted">None touch this capability</p>}
          {initiatives.map((i) => (
            <label key={i.slug} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={selectedInitiatives.has(i.slug)}
                onChange={() => toggle(selectedInitiatives, setSelectedInitiatives, i.slug)}
              />
              {i.title ?? i.slug}
            </label>
          ))}
        </fieldset>
        <fieldset className="flex flex-col gap-1">
          <legend className="text-xs uppercase text-ink-muted">Issues</legend>
          {issues.length === 0 && <p className="text-xs text-ink-muted">None touch this capability</p>}
          {issues.map((w) => (
            <label key={w.id} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={selectedIssues.has(w.id)}
                onChange={() => toggle(selectedIssues, setSelectedIssues, w.id)}
              />
              {w.title ?? w.id}
            </label>
          ))}
        </fieldset>
        <Field label="Move to" htmlFor="remap-target">
          <Select id="remap-target" value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">— choose a capability —</option>
            {allCapabilities.filter((c) => c.slug !== capability.slug).map((c) => (
              <option key={c.id} value={c.slug}>{c.title ?? c.slug}</option>
            ))}
          </Select>
        </Field>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button
            disabled={!target || (selectedInitiatives.size === 0 && selectedIssues.size === 0) || moving}
            onClick={() => void moveSelected()}
          >
            Move selected
          </Button>
        </div>
      </div>
    </Dialog>
  )
}
