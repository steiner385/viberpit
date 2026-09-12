// "+ Capability" on a product, "+ Sub-capability" on a capability: the same
// form, which is why it is one component — the capability level only presets
// the parent (R8c-1). It sends a single `create` op, which opens the product's
// map-changes branch and PR on the first edit and appends to it after that; the
// new capability is proposed until that PR merges.
import { useRef, useState } from 'react'
import { toast } from 'sonner'
import type { ProductNode } from '@/api/roadmap'
import type { MapChangeInput } from '@/api/pm'
import { useEditMapChanges, mapChangesErrorMessage } from '@/api/hooks'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Select } from '@/ui/Select'
import { Textarea } from '@/ui/Textarea'
import { Button } from '@/ui/Button'
import { CAPABILITY } from '@/features/products/vocab'
import { flattenCapabilities } from '../lookup'
import { sanitize, slugify } from '../slug'

export function NewCapabilityDialog({ product, open, onClose, parentSlug }: {
  product: ProductNode
  open: boolean
  onClose: () => void
  /** Preset parent — the capability level opens the same dialog for a child. */
  parentSlug?: string
}) {
  const titleRef = useRef<HTMLInputElement>(null)
  const edit = useEditMapChanges(product.slug)

  const [title, setTitle] = useState('')
  const [slug, setSlug] = useState('')
  const [slugTouched, setSlugTouched] = useState(false)
  const [summary, setSummary] = useState('')
  const [parent, setParent] = useState(parentSlug ?? '')
  const [failure, setFailure] = useState<string | null>(null)

  // The slug follows the title until the user takes it over — the same rule the
  // initiative dialog uses, from the same module (R8c-6).
  const shownSlug = slugTouched ? slug : slugify(title)
  const effectiveSlug = slugify(shownSlug)
  const canCreate = title.trim() !== '' && effectiveSlug !== '' && !edit.isPending
  // A title of "!!!" (or a slug edited down to punctuation) normalises to
  // nothing; say why Create is disabled rather than leaving it greyed out.
  const slugError = title.trim() !== '' && effectiveSlug === ''
    ? 'Add a letter or number to the slug'
    : undefined

  const create = async () => {
    const trimmedTitle = title.trim()
    const input: MapChangeInput = {
      op: 'create',
      slug: effectiveSlug,
      title: trimmedTitle,
      // Explicitly null at the root: `parentSlug` is what decides where the
      // capability lands, and omitting it would read as "unchanged" elsewhere.
      parentSlug: parent || null,
      // Omitted rather than sent blank — the server refuses an empty summary,
      // and a capability with no words yet is a real state.
      ...(summary.trim() ? { summary: summary.trim() } : {}),
    }
    setFailure(null)
    let result
    try {
      result = await edit.mutateAsync(input)
    } catch (e) {
      setFailure(mapChangesErrorMessage(e, `Could not add the ${CAPABILITY.singular.toLowerCase()}.`))
      return
    }
    onClose()
    // R8c-10: it does NOT navigate to the new capability. The commit lands at
    // once, but the MAP only learns about it on the next pm-sync pass, so its
    // page would show "Not found — may have been renamed" for that whole window
    // and a retry would 422 `slug_taken`. The toast says where it went instead.
    toast.success(
      `Added "${trimmedTitle}" — it appears on the map at the next sync, in PR #${result.pointer.prNumber ?? '…'}`,
    )
  }

  return (
    <Dialog open={open} onClose={onClose} title={`New ${CAPABILITY.singular.toLowerCase()}`} size="md" initialFocus={titleRef}>
      <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); if (canCreate) void create() }}>
        <Field label="Title" htmlFor="nc-title" required>
          <Input ref={titleRef} id="nc-title" value={title} onChange={(e) => setTitle(e.target.value)} />
        </Field>
        <Field label="Slug" htmlFor="nc-slug" help="Names the feature doc and the capability's page" error={slugError} required>
          <Input
            id="nc-slug"
            invalid={!!slugError}
            value={shownSlug}
            onChange={(e) => { setSlugTouched(true); setSlug(sanitize(e.target.value)) }}
          />
        </Field>
        <Field label={`Parent ${CAPABILITY.singular.toLowerCase()}`} htmlFor="nc-parent">
          <Select id="nc-parent" value={parent} onChange={(e) => setParent(e.target.value)}>
            <option value="">— root —</option>
            {flattenCapabilities(product.capabilities).map(({ node, depth }) => (
              <option key={node.slug} value={node.slug}>{'  '.repeat(depth)}{node.title ?? node.slug}</option>
            ))}
          </Select>
        </Field>
        <Field label="Summary" htmlFor="nc-summary" help="Optional — the first line of the feature doc">
          <Textarea id="nc-summary" autoGrow rows={3} value={summary} onChange={(e) => setSummary(e.target.value)} />
        </Field>
        {failure && <p role="alert" className="text-sm text-danger">{failure}</p>}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" size="lg" disabled={!canCreate} loading={edit.isPending}>Create</Button>
        </div>
      </form>
    </Dialog>
  )
}
