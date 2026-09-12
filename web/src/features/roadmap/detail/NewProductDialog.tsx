// "+ Product" on the portfolio: the one place a human registers a product, and
// therefore the entrance to everything else on the roadmap — nothing is mirrored,
// classified or tiered until a product exists. It asks for the two things the
// registry cannot derive (a name and the repository to mirror) and one it is
// happy without (a description).
//
// The sibling of `NewInitiativeDialog`, down to the primitives and the shape of
// the submit handler.
import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type { CreateProjectInput } from '@/api/pm'
import { useCreateProject } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Button } from '@/ui/Button'
import { PRODUCT } from '@/features/products/vocab'
import { productPath } from '../roadmapNavState'

/** `owner/repo` — the only shape the backfill loop can mirror from. */
const REPO_RE = /^[^/\s]+\/[^/\s]+$/

export function NewProductDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate()
  // The dialog exists to be typed into; the name is where that starts.
  const nameRef = useRef<HTMLInputElement>(null)
  const create = useCreateProject()

  const [name, setName] = useState('')
  const [repo, setRepo] = useState('')
  const [description, setDescription] = useState('')

  const trimmedRepo = repo.trim()
  const repoValid = REPO_RE.test(trimmedRepo)
  // Only complain about what has been typed: an untouched field is not an error.
  const repoError = trimmedRepo !== '' && !repoValid
    ? 'Use owner/repo, e.g. steiner385/viberpit'
    : undefined
  const canSubmit = name.trim() !== '' && repoValid && !create.isPending

  return (
    <Dialog open={open} onClose={onClose} title={PRODUCT.new} size="md" initialFocus={nameRef}>
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault()
          if (!canSubmit) return
          const input: CreateProjectInput = {
            name: name.trim(),
            repos: [{ repo: trimmedRepo, isPrimary: true }],
            // Omitted rather than sent empty: the registry stores null for "not
            // said yet", and an empty string would read as a description of "".
            ...(description.trim() ? { description: description.trim() } : {}),
          }
          create.mutate(input, {
            onSuccess: (created) => {
              onClose()
              // The server slugs the name; a payload without one (an older
              // build, a name that slugged to nothing) leaves the portfolio
              // open, which now lists the product anyway.
              if (created.slug) navigate(productPath(created.slug))
            },
          })
        }}
      >
        <Field label="Name" htmlFor="np-name" required>
          <Input ref={nameRef} id="np-name" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field
          label="Primary repository"
          htmlFor="np-repo"
          help="The repository whose backlog is mirrored"
          error={repoError}
          required
        >
          <Input
            id="np-repo"
            placeholder="owner/repo"
            invalid={!!repoError}
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
          />
        </Field>
        <Field label="Description" htmlFor="np-description">
          <Input id="np-description" value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        {create.isError && (
          <p role="alert" className="text-sm text-danger">
            {extractMutationErrorMessage(create.error, `Could not create the ${PRODUCT.singular.toLowerCase()}.`)}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          {/* `lg` is the only size with a 44px touch target — the house rule for
              a primary confirm in a dialog footer. */}
          <Button type="submit" size="lg" disabled={!canSubmit} loading={create.isPending}>Create</Button>
        </div>
      </form>
    </Dialog>
  )
}
