// "Edit" on an initiative: the title and its place on the map (roadmap spec §8,
// "Create initiative; set primary and touched capabilities" — the edit half).
// The primary picks the product, so the touched checkboxes are always the REST
// of that product's tree; a primary from another product starts the set over,
// because the server refuses a set that spans products (422).
import { useMemo, useRef, useState } from 'react'
import type { Initiative, UpdateInitiativeInput } from '@/api/sdlc'
import type { CapabilityNode, ProductNode } from '@/api/roadmap'
import { useUpdateInitiative } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Select } from '@/ui/Select'
import { Checkbox } from '@/ui/Checkbox'
import { Button } from '@/ui/Button'
import { CAPABILITY, INITIATIVE } from '@/features/products/vocab'
import { flattenCapabilities } from '../lookup'

function productOfCapability(products: ProductNode[], capId: string): ProductNode | null {
  return products.find((p) => flattenCapabilities(p.capabilities).some(({ node }) => node.id === capId)) ?? null
}

export function EditInitiativeDialog({ initiative, products, open, onClose }: {
  initiative: Initiative
  /** Every product on the map — the primary may move the initiative between them. */
  products: ProductNode[]
  open: boolean
  onClose: () => void
}) {
  const update = useUpdateInitiative()
  const titleRef = useRef<HTMLInputElement>(null)
  const seedTitle = initiative.title ?? ''
  const seedPrimary = initiative.primaryCapability?.id ?? ''
  const seedTouched = initiative.capabilities.filter((c) => !c.isPrimary).map((c) => c.id)

  const [title, setTitle] = useState(seedTitle)
  const [primary, setPrimary] = useState(seedPrimary)
  const [touched, setTouched] = useState<string[]>(seedTouched)

  // The product the primary belongs to; the touched list is its tree minus the primary.
  const product = primary ? productOfCapability(products, primary) : null
  const touchable = useMemo(
    () => (product ? flattenCapabilities(product.capabilities).filter(({ node }) => node.id !== primary) : []),
    [product, primary],
  )

  const trimmed = title.trim()
  const titleChanged = trimmed !== seedTitle
  const setChanged = primary !== seedPrimary || [...touched].sort().join() !== [...seedTouched].sort().join()
  const canSave = trimmed !== '' && (titleChanged || setChanged) && !update.isPending
  const titleError = trimmed === '' ? 'Give it a title' : undefined

  const save = () => {
    const input: UpdateInitiativeInput = {}
    if (titleChanged) input.title = trimmed
    // Never send an empty primary: the server would read it as "no primary",
    // clear the product with it, and an initiative with no product is on no
    // node of the map. This form CHANGES an initiative's home, never removes it.
    if (primary !== '' && primary !== seedPrimary) input.primaryCapabilityId = primary
    // The set is sent whole whenever any part of it moved — it replaces, not merges.
    if (setChanged && primary !== '') input.capabilityIds = [primary, ...touched]
    update.mutate({ slug: initiative.slug, input }, { onSuccess: onClose })
  }

  const choosePrimary = (id: string) => {
    const next = productOfCapability(products, id)
    // Same product: the OUTGOING primary is still a capability the initiative
    // touches, so it joins the touched set as the incoming one leaves it.
    // Another product: the set cannot span products, so it starts over.
    setTouched((cur) => (next && product && next.slug === product.slug
      ? [...cur.filter((x) => x !== id), ...(primary && primary !== id ? [primary] : [])]
      : []))
    setPrimary(id)
  }

  return (
    <Dialog open={open} onClose={onClose} title={`Edit ${INITIATIVE.singular.toLowerCase()}`} size="md" initialFocus={titleRef}>
      <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); if (canSave) save() }}>
        <Field label="Title" htmlFor="ei-title" error={titleError} required>
          <Input ref={titleRef} id="ei-title" invalid={!!titleError} value={title} onChange={(e) => setTitle(e.target.value)} />
        </Field>
        <Field label={`Primary ${CAPABILITY.singular.toLowerCase()}`} htmlFor="ei-primary" help="Decides which product the initiative belongs to">
          {/* No "none" option: an initiative's home is changed here, not
              removed. Clearing the primary clears the product with it, which
              takes the initiative off every node of the map; unticking is how
              the touched set is emptied. */}
          <Select id="ei-primary" value={primary} onChange={(e) => choosePrimary(e.target.value)}>
            {/* An initiative that has no home yet seeds `''`; without an option
                for it the select renders blank. The placeholder is disabled, so
                it can be the starting point but never the choice. */}
            <option value="" disabled>{`— choose a ${CAPABILITY.singular.toLowerCase()} —`}</option>
            {products.map((p) => (
              <optgroup key={p.slug} label={p.name ?? p.slug}>
                {flattenCapabilities(p.capabilities).map(({ node, depth }) => (
                  <option key={node.id} value={node.id}>{'  '.repeat(depth)}{node.title ?? node.slug}</option>
                ))}
              </optgroup>
            ))}
          </Select>
        </Field>
        {touchable.length > 0 && (
          <fieldset className="flex flex-col gap-1">
            <legend className="text-sm font-medium text-ink">Also touches</legend>
            {touchable.map(({ node, depth }: { node: CapabilityNode; depth: number }) => (
              <Checkbox
                key={node.id}
                id={`ei-touch-${node.id}`}
                label={node.title ?? node.slug}
                className={depth > 0 ? 'ml-4' : undefined}
                checked={touched.includes(node.id)}
                onChange={(next) => setTouched((cur) =>
                  next ? [...cur, node.id] : cur.filter((x) => x !== node.id))}
              />
            ))}
          </fieldset>
        )}
        {update.isError && (
          <p role="alert" className="text-sm text-danger">
            {extractMutationErrorMessage(update.error, `Could not update the ${INITIATIVE.singular.toLowerCase()}.`)}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" size="lg" disabled={!canSave} loading={update.isPending}>Save</Button>
        </div>
      </form>
    </Dialog>
  )
}
