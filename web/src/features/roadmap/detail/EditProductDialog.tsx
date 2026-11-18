// The rename counterpart to `NewProductDialog`: same primitives, same shape of
// submit handler, but a PATCH against an existing product instead of a POST
// that creates one. The slug is identity — changing it is refused server-side
// while a map-changes branch/PR is open (it is named from the CURRENT slug), so
// a 409 from that guard surfaces as the mutation's own error message rather
// than a special-cased banner here. Repositories are managed on the product's
// Overview (`ProductReposPanel`), not here.
import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type { Project, UpdateProjectInput } from '@/api/pm'
import { useUpdateProject } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Button } from '@/ui/Button'
import { productPath } from '../roadmapNavState'

/** Branch-safe: the server's `SLUG_RE` (`server/pm/map_changes_ops.py`) —
 *  the slug goes straight into a git ref, unencoded. */
const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,63}$/

export function EditProductDialog({
  project, open, onClose,
}: { project: Project; open: boolean; onClose: () => void }) {
  const navigate = useNavigate()
  const nameRef = useRef<HTMLInputElement>(null)
  const update = useUpdateProject(project.slug ?? '')

  const [name, setName] = useState(project.name ?? '')
  const [slug, setSlug] = useState(project.slug ?? '')

  const trimmedName = name.trim()
  const trimmedSlug = slug.trim()
  const slugValid = SLUG_RE.test(trimmedSlug)
  const slugError = trimmedSlug !== '' && !slugValid
    ? 'Lowercase letters, digits and hyphens only'
    : undefined
  const canSubmit = trimmedName !== '' && slugValid && !update.isPending

  return (
    <Dialog open={open} onClose={onClose} title="Edit product" size="md" initialFocus={nameRef}>
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault()
          if (!canSubmit) return
          const input: UpdateProjectInput = {
            ...(trimmedName !== (project.name ?? '') ? { name: trimmedName } : {}),
            ...(trimmedSlug !== (project.slug ?? '') ? { slug: trimmedSlug } : {}),
          }
          if (Object.keys(input).length === 0) {
            onClose()
            return
          }
          update.mutate(input, {
            onSuccess: (saved) => {
              onClose()
              if (saved.slug && saved.slug !== project.slug) navigate(productPath(saved.slug))
            },
          })
        }}
      >
        <Field label="Name" htmlFor="ep-name" required>
          <Input ref={nameRef} id="ep-name" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field
          label="Slug"
          htmlFor="ep-slug"
          help="Used in URLs and the map-changes branch name"
          error={slugError}
          required
        >
          <Input id="ep-slug" invalid={!!slugError} value={slug} onChange={(e) => setSlug(e.target.value)} />
        </Field>
        {update.isError && (
          <p role="alert" className="text-sm text-danger">
            {extractMutationErrorMessage(update.error, 'Could not update the product.')}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" size="lg" disabled={!canSubmit} loading={update.isPending}>Save</Button>
        </div>
      </form>
    </Dialog>
  )
}
