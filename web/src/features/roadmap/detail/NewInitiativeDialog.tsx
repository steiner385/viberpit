// "+ Initiative" on a product: the one place a human starts work from the
// roadmap. Everything it needs is already on screen — the product's repos and
// its capability tree — so the form asks for a title and nothing else it can
// derive (finish-line spec §7).
import { useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type { CapabilityNode, ProductNode } from '@/api/roadmap'
import type { CreateInitiativeInput } from '@/api/sdlc'
import { useCreateInitiative, useProject } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Select } from '@/ui/Select'
import { Button } from '@/ui/Button'
import { CAPABILITY, INITIATIVE } from '@/features/products/vocab'
import { initiativePath } from '../roadmapNavState'

/** What the user is typing, kept slug-shaped: a trailing separator survives so
 *  the space between two words can be typed at all. */
const sanitize = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-/, '')
/** The final slug — `sanitize` plus the trailing separator a finished word never has. */
const slugify = (s: string) => sanitize(s.trim()).replace(/-$/, '')

/** The capability tree as an indented flat list, so a `select` can offer the
 *  whole tree without pretending it is flat. */
function flatten(nodes: CapabilityNode[], depth = 0): { node: CapabilityNode; depth: number }[] {
  return nodes.flatMap((n) => [{ node: n, depth }, ...flatten(n.children, depth + 1)])
}

export function NewInitiativeDialog({ product, open, onClose, defaultCapabilityId }: {
  product: ProductNode
  open: boolean
  onClose: () => void
  /** Pre-selects a capability — the capability level opens the same dialog. */
  defaultCapabilityId?: string
}) {
  const navigate = useNavigate()
  // The dialog exists to be typed into; the title is where that starts.
  const titleRef = useRef<HTMLInputElement>(null)
  const create = useCreateInitiative()
  // Same query key the product page already holds, so opening the dialog costs
  // no fetch; the repos are the ones the product is actually mirrored from.
  const { data: project } = useProject(product.slug)
  const repos = project?.repos.map((r) => r.repo).filter((r): r is string => !!r) ?? []
  const caps = useMemo(() => flatten(product.capabilities), [product])

  const [title, setTitle] = useState('')
  const [slug, setSlug] = useState('')
  const [slugTouched, setSlugTouched] = useState(false)
  const [repo, setRepo] = useState('')
  const [capId, setCapId] = useState(defaultCapabilityId ?? '')

  // The slug follows the title until the user takes it over, and the mirrored
  // value is fully normalised (the title carries its own spaces); only the field
  // the user has taken over keeps a half-typed separator. The repo defaults to
  // the primary one, which the project lists first.
  const shownSlug = slugTouched ? slug : slugify(title)
  const effectiveSlug = slugify(shownSlug)
  const effectiveRepo = repo || repos[0] || ''
  const canSubmit = title.trim() !== '' && effectiveSlug !== '' && effectiveRepo !== '' && !create.isPending

  return (
    <Dialog open={open} onClose={onClose} title={INITIATIVE.new} size="md" initialFocus={titleRef}>
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault()
          if (!canSubmit) return
          const input: CreateInitiativeInput = {
            slug: effectiveSlug,
            title: title.trim(),
            repo: effectiveRepo,
            origin: 'human',
            // Omitted rather than sent empty: an initiative with no home yet is
            // a real state the roadmap already renders ("needs a home").
            ...(capId ? { primaryCapabilityId: capId, capabilityIds: [capId] } : {}),
          }
          create.mutate(input, {
            onSuccess: (created) => {
              onClose()
              navigate(initiativePath(created.slug))
            },
          })
        }}
      >
        <Field label="Title" htmlFor="ni-title" required>
          <Input ref={titleRef} id="ni-title" value={title} onChange={(e) => setTitle(e.target.value)} />
        </Field>
        <Field label="Slug" htmlFor="ni-slug" help="Used in URLs and branch names" required>
          <Input
            id="ni-slug"
            value={shownSlug}
            onChange={(e) => { setSlugTouched(true); setSlug(sanitize(e.target.value)) }}
          />
        </Field>
        <Field label="Repository" htmlFor="ni-repo" required>
          <Select id="ni-repo" value={effectiveRepo} onChange={(e) => setRepo(e.target.value)}>
            {repos.length === 0 && <option value="">No repository is attached to this product</option>}
            {repos.map((r) => <option key={r} value={r}>{r}</option>)}
          </Select>
        </Field>
        <Field label={`Primary ${CAPABILITY.singular.toLowerCase()}`} htmlFor="ni-cap">
          <Select id="ni-cap" value={capId} onChange={(e) => setCapId(e.target.value)}>
            <option value="">— none yet —</option>
            {caps.map(({ node, depth }) => (
              <option key={node.id} value={node.id}>
                {'  '.repeat(depth)}{node.title ?? node.slug}
              </option>
            ))}
          </Select>
        </Field>
        {create.isError && (
          <p role="alert" className="text-sm text-danger">
            {extractMutationErrorMessage(create.error, `Could not create the ${INITIATIVE.singular.toLowerCase()}.`)}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={!canSubmit} loading={create.isPending}>Create</Button>
        </div>
      </form>
    </Dialog>
  )
}
