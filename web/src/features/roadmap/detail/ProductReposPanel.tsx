// A product's repositories, managed in place (spec 2026-11-18 product repo
// lifecycle, "UI"): add one, make an active one primary, retire one (with a
// reason, what superseded it, and — when it is the primary — the new primary),
// un-retire one, and map issues that moved between repos. It replaces the
// read-only pill list and the edit dialog's "Primary repository" field.
//
// Every primary-moving action carries the primary this panel DISPLAYED as
// `expectedPrimary`, so a stale view is refused (409 `primary_changed`) rather
// than acting on a primary the user never saw.
import { useState } from 'react'
import {
  REPO_FORMAT_HINT, REPO_RE, isActiveRepo, type Project, type ProjectRepoInfo,
} from '@/api/pm'
import { useAddProjectRepo, useRetireRepo, useSetPrimaryRepo, useUnretireRepo } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Select } from '@/ui/Select'
import { cn } from '@/lib/utils'
import { MovedIssuesDialog } from './MovedIssuesDialog'

/** "Retired · <reason> · → <superseded by>", each part only when known. */
function retiredLabel(r: ProjectRepoInfo): string {
  return ['Retired', r.retiredReason, r.supersededBy ? `→ ${r.supersededBy}` : null]
    .filter(Boolean).join(' · ')
}

export function ProductReposPanel({ project }: { project: Project }) {
  const slug = project.slug ?? ''
  const add = useAddProjectRepo(slug)
  const setPrimary = useSetPrimaryRepo(slug)
  const unretire = useUnretireRepo(slug)
  const [newRepo, setNewRepo] = useState('')
  const [retiring, setRetiring] = useState<string | null>(null)
  const [movedOpen, setMovedOpen] = useState(false)

  const repos = project.repos.filter((r): r is ProjectRepoInfo & { repo: string } => !!r.repo)
  const activeCount = repos.filter(isActiveRepo).length
  // The ACTIVE primary only — a retired row's leftover flag is not "the primary".
  const displayedPrimary = repos.find((r) => r.isPrimary && isActiveRepo(r))?.repo ?? null

  const trimmed = newRepo.trim()
  const repoError = trimmed !== '' && !REPO_RE.test(trimmed) ? REPO_FORMAT_HINT : undefined
  const canAdd = REPO_RE.test(trimmed) && !add.isPending
  // One inline line for whichever row action failed last; the retire dialog
  // renders its own.
  const failed = [add, setPrimary, unretire].find((m) => m.isError)

  return (
    <div className="flex flex-col gap-2">
      {repos.length > 0 && (
        <ul className="flex flex-col gap-1" aria-label="Repositories">
          {repos.map((r) => {
            const active = isActiveRepo(r)
            const primary = active && r.repo === displayedPrimary
            return (
              <li
                key={r.id}
                className={cn(
                  'flex flex-wrap items-center gap-2 rounded-md border border-edge px-2 py-1 text-xs',
                  active ? 'text-ink' : 'text-ink-muted',
                )}
              >
                <span className={cn('font-medium', !active && 'line-through')}>{r.repo}</span>
                {primary && <Badge tone="accent">primary</Badge>}
                {active && !r.backfillDone && <Badge>backfilling</Badge>}
                {!active && <span>{retiredLabel(r)}</span>}
                <span className="ml-auto flex gap-1">
                  {active && !primary && (
                    <Button
                      size="sm" variant="ghost" aria-label={`Make ${r.repo} primary`}
                      disabled={setPrimary.isPending}
                      onClick={() => setPrimary.mutate({ repo: r.repo, expectedPrimary: displayedPrimary })}
                    >
                      Make primary
                    </Button>
                  )}
                  {active && (
                    <Button
                      size="sm" variant="ghost" aria-label={`Retire ${r.repo}`}
                      // The server refuses `last_active_repo`; say so up front.
                      disabled={activeCount <= 1}
                      title={activeCount <= 1 ? 'Add another repository before retiring the last one' : undefined}
                      onClick={() => setRetiring(r.repo)}
                    >
                      Retire…
                    </Button>
                  )}
                  {!active && (
                    <Button
                      size="sm" variant="ghost" aria-label={`Un-retire ${r.repo}`}
                      disabled={unretire.isPending}
                      onClick={() => unretire.mutate(r.repo)}
                    >
                      Un-retire
                    </Button>
                  )}
                </span>
              </li>
            )
          })}
        </ul>
      )}
      {failed && (
        <p role="alert" className="text-xs text-danger">
          {extractMutationErrorMessage(failed.error, 'Could not update the repositories.')}
        </p>
      )}
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (!canAdd) return
          add.mutate({ repo: trimmed }, { onSuccess: () => setNewRepo('') })
        }}
      >
        <Field label="Add repository" htmlFor="pr-add" error={repoError} className="min-w-0 flex-1">
          <Input
            id="pr-add" size="sm" placeholder="owner/repo" invalid={!!repoError}
            value={newRepo} onChange={(e) => setNewRepo(e.target.value)}
          />
        </Field>
        <Button type="submit" size="sm" variant="secondary" disabled={!canAdd} loading={add.isPending}>Add</Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => setMovedOpen(true)}>Moved issues…</Button>
      </form>
      {/* Mounted only while open, so each opening starts from blank fields. */}
      {retiring && (
        <RetireRepoDialog
          project={project} repo={retiring} displayedPrimary={displayedPrimary}
          onClose={() => setRetiring(null)}
        />
      )}
      {movedOpen && <MovedIssuesDialog project={project} open onClose={() => setMovedOpen(false)} />}
    </div>
  )
}

function RetireRepoDialog({
  project, repo, displayedPrimary, onClose,
}: { project: Project; repo: string; displayedPrimary: string | null; onClose: () => void }) {
  const retire = useRetireRepo(project.slug ?? '')
  const [reason, setReason] = useState('')
  const [supersededBy, setSupersededBy] = useState('')
  const [newPrimary, setNewPrimary] = useState('')
  const isPrimary = repo === displayedPrimary
  // Only ACTIVE repos can take over, and never the one being retired.
  const candidates = project.repos
    .filter((r) => isActiveRepo(r) && !!r.repo && r.repo !== repo)
    .map((r) => r.repo as string)
  const canSubmit = (!isPrimary || candidates.includes(newPrimary)) && !retire.isPending

  return (
    <Dialog open onClose={onClose} title={`Retire ${repo}`} size="md">
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault()
          if (!canSubmit) return
          retire.mutate({
            repo,
            reason: reason.trim() || null,
            supersededBy: supersededBy || null,
            newPrimary: isPrimary ? newPrimary : null,
            expectedPrimary: displayedPrimary,
          }, { onSuccess: onClose })
        }}
      >
        <p className="text-sm text-ink-muted">
          Its issues leave the queue and it stops syncing. It stays listed, and can be un-retired.
        </p>
        <Field label="Reason" htmlFor="rr-reason">
          <Input id="rr-reason" placeholder="e.g. consolidated" value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
        <Field label="Superseded by" htmlFor="rr-superseded">
          <Select id="rr-superseded" value={supersededBy} onChange={(e) => setSupersededBy(e.target.value)}>
            <option value="">None</option>
            {candidates.map((c) => <option key={c} value={c}>{c}</option>)}
          </Select>
        </Field>
        {isPrimary && (
          <Field label="New primary" htmlFor="rr-primary" help="This is the primary repository; choose its replacement" required>
            <Select id="rr-primary" value={newPrimary} onChange={(e) => setNewPrimary(e.target.value)}>
              <option value="">Choose a repository…</option>
              {candidates.map((c) => <option key={c} value={c}>{c}</option>)}
            </Select>
          </Field>
        )}
        {retire.isError && (
          <p role="alert" className="text-sm text-danger">
            {extractMutationErrorMessage(retire.error, 'Could not retire the repository.')}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" size="lg" variant="danger" disabled={!canSubmit} loading={retire.isPending}>Retire</Button>
        </div>
      </form>
    </Dialog>
  )
}
