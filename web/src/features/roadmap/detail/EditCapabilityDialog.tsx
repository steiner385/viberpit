// "Edit" on a capability: its title, where it sits in the tree, and its summary
// (spec §8 "Add / rename capability, reparent, edit summary"). One dialog, but
// not one request: the server takes exactly one op per call and turns each into
// its own commit on the product's map-changes branch, so Save sends the changed
// ones in sequence — rename, then reparent, then summary — and stops at the
// first refusal with the form still holding what the user typed (R8c-1/R8c-5).
import { useMemo, useRef, useState } from 'react'
import type { CapabilityNode, ProductNode } from '@/api/roadmap'
import type { MapChangeInput } from '@/api/pm'
import { useEditMapChanges, mapChangesErrorMessage } from '@/api/hooks'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Select } from '@/ui/Select'
import { Textarea } from '@/ui/Textarea'
import { Button } from '@/ui/Button'
import { CAPABILITY } from '@/features/products/vocab'
import { findCapability, flattenCapabilities } from '../lookup'

/** The capability and everything under it — the parents it may never be given,
 *  because each would close a cycle the server refuses with `parent_cycle`.
 *  Read from the PRODUCT's copy of the node: the prop may be a narrowed view
 *  that carries no children. */
function selfAndDescendants(product: ProductNode, capability: CapabilityNode): Set<string> {
  const node = findCapability(product, capability.slug) ?? capability
  const walk = (n: CapabilityNode): string[] => [n.slug, ...n.children.flatMap(walk)]
  return new Set(walk(node))
}

export function EditCapabilityDialog({ product, capability, open, onClose }: {
  product: ProductNode
  capability: CapabilityNode
  open: boolean
  onClose: () => void
}) {
  const edit = useEditMapChanges(product.slug)
  const titleRef = useRef<HTMLInputElement>(null)
  const seedTitle = capability.title ?? ''
  const seedParent = capability.parentSlug ?? ''
  const seedSummary = capability.summary ?? ''

  const [title, setTitle] = useState(seedTitle)
  const [parent, setParent] = useState(seedParent)
  const [summary, setSummary] = useState(seedSummary)
  // The refusal that stopped the sequence. Local rather than the mutation's own
  // `isError`: a later op may succeed after an earlier one failed, which would
  // clear the flag while the edit as a whole did not land.
  const [failure, setFailure] = useState<string | null>(null)

  const blocked = useMemo(() => selfAndDescendants(product, capability), [product, capability])
  const parents = useMemo(
    () => flattenCapabilities(product.capabilities).filter(({ node }) => !blocked.has(node.slug)),
    [product, blocked],
  )

  const trimmedTitle = title.trim()
  const trimmedSummary = summary.trim()
  const titleChanged = trimmedTitle !== seedTitle
  const parentChanged = parent !== seedParent
  const summaryChanged = trimmedSummary !== seedSummary
  const titleError = trimmedTitle === '' ? 'Give it a title' : undefined
  // The server refuses a blank summary (`bad_summary`), and there is no op that
  // removes one — a summary is replaced, never deleted. Said here rather than
  // sent and bounced back as a 422.
  const summaryError = summaryChanged && trimmedSummary === ''
    ? 'A summary cannot be emptied, only replaced'
    : undefined
  const canSave = !titleError && !summaryError
    && (titleChanged || parentChanged || summaryChanged) && !edit.isPending

  const save = async () => {
    const ops: MapChangeInput[] = []
    if (titleChanged) ops.push({ op: 'rename', slug: capability.slug, title: trimmedTitle })
    if (parentChanged) ops.push({ op: 'reparent', slug: capability.slug, parentSlug: parent || null })
    if (summaryChanged) ops.push({ op: 'summary', slug: capability.slug, summary: trimmedSummary })
    setFailure(null)
    for (const op of ops) {
      try {
        await edit.mutateAsync(op)
      } catch (e) {
        // Whatever landed before this stays landed — the branch is a sequence of
        // commits, not a transaction. The dialog stays open on the user's own
        // values so a retry sends only what is still different.
        setFailure(mapChangesErrorMessage(e, `Could not save the ${CAPABILITY.singular.toLowerCase()}.`))
        return
      }
    }
    onClose()
  }

  return (
    <Dialog open={open} onClose={onClose} title={`Edit ${CAPABILITY.singular.toLowerCase()}`} size="md" initialFocus={titleRef}>
      <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); if (canSave) void save() }}>
        <Field label="Title" htmlFor="ec-title" error={titleError} required>
          <Input ref={titleRef} id="ec-title" invalid={!!titleError} value={title} onChange={(e) => setTitle(e.target.value)} />
        </Field>
        <Field label={`Parent ${CAPABILITY.singular.toLowerCase()}`} htmlFor="ec-parent" help="Where it sits in the tree">
          <Select id="ec-parent" value={parent} onChange={(e) => setParent(e.target.value)}>
            {/* A top-level capability is a real choice, not the absence of one. */}
            <option value="">— root —</option>
            {parents.map(({ node, depth }) => (
              <option key={node.slug} value={node.slug}>{'  '.repeat(depth)}{node.title ?? node.slug}</option>
            ))}
          </Select>
        </Field>
        <Field label="Summary" htmlFor="ec-summary" error={summaryError} help="The first line of the feature doc">
          <Textarea
            id="ec-summary"
            autoGrow
            rows={3}
            invalid={!!summaryError}
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
          />
        </Field>
        {failure && <p role="alert" className="text-sm text-danger">{failure}</p>}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" size="lg" disabled={!canSave} loading={edit.isPending}>Save</Button>
        </div>
      </form>
    </Dialog>
  )
}
