// The rename/repoint counterpart to `NewProductDialog`: same primitives, same
// shape of submit handler, but a PATCH against an existing product instead of
// a POST that creates one. Slug and the primary repo are both identity —
// changing either is refused server-side while a map-changes branch/PR is
// open (it is named and hosted from the CURRENT slug/repo), so a 409 from
// that guard surfaces as the mutation's own error message rather than a
// special-cased banner here.
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

/** `owner/repo` — the only shape the backfill loop can mirror from. */
const REPO_RE = /^[^/\s]+\/[^/\s]+$/
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
  const primaryRepo = project.repos.find((r) => r.isPrimary)?.repo ?? project.repos[0]?.repo ?? ''
  const [repo, setRepo] = useState(primaryRepo)

  const trimmedName = name.trim()
  const trimmedSlug = slug.trim()
  const trimmedRepo = repo.trim()
  const slugValid = SLUG_RE.test(trimmedSlug)
  const slugError = trimmedSlug !== '' && !slugValid
    ? 'Lowercase letters, digits and hyphens only'
    : undefined
  const repoValid = REPO_RE.test(trimmedRepo)
  const repoError = trimmedRepo !== '' && !repoValid
    ? 'Use owner/repo, e.g. steiner385/viberpit'
    : undefined
  const canSubmit = trimmedName !== '' && slugValid && repoValid && !update.isPending

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
            ...(trimmedRepo !== primaryRepo ? { repo: trimmedRepo } : {}),
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
        <Field
          label="Primary repository"
          htmlFor="ep-repo"
          help="Repointing to a different repo re-mirrors its backlog from scratch"
          error={repoError}
          required
        >
          <Input
            id="ep-repo"
            placeholder="owner/repo"
            invalid={!!repoError}
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
          />
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
