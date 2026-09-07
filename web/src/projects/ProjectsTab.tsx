/**
 * Products registry (PM product layer, phase 1): project cards + New Project
 * form + sync-now. Deliberately minimal — the onboarding wizard, board, and
 * inbox land in later phases (design 2026-08-31 §10).
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { useCreateProject, useProjects, useSyncProject, useTriage } from '@/api/hooks'
import type { Project } from '@/api/pm'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { NAV_BY_ID } from '@/shell/navDefs'
import { PRODUCT, onboardingStateLabel } from '@/features/products/vocab'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { Button } from '@/ui/Button'

import { DependencyPanel } from './DependencyPanel'
import { OnboardingWizard } from './OnboardingWizard'
import './projects.css'

function NewProjectForm({ onClose }: { onClose: () => void }) {
  const create = useCreateProject()
  const [name, setName] = useState('')
  const [repo, setRepo] = useState('')
  const canSubmit = name.trim() !== '' && /^[^/\s]+\/[^/\s]+$/.test(repo.trim())
  return (
    <form
      className="pm-new-project"
      onSubmit={(e) => {
        e.preventDefault()
        if (!canSubmit) return
        create.mutate(
          { name: name.trim(), repos: [{ repo: repo.trim(), isPrimary: true }] },
          { onSuccess: onClose },
        )
      }}
    >
      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)}
               placeholder="My product" />
      </label>
      <label>
        Primary repo
        <input value={repo} onChange={(e) => setRepo(e.target.value)}
               placeholder="owner/repo" />
      </label>
      {create.isError && (
        <p role="alert" className="pm-error">
          {extractMutationErrorMessage(create.error, 'Could not create product')}
        </p>
      )}
      <div className="pm-form-actions">
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="submit" disabled={!canSubmit || create.isPending}>
          {create.isPending ? 'Creating…' : 'Add product'}
        </button>
      </div>
    </form>
  )
}

function ProjectCard({ project }: { project: Project }) {
  const sync = useSyncProject()
  return (
    <div className="pm-project-card">
      <div className="pm-project-head">
        <h3>{project.name ?? project.slug}</h3>
        <span className={`pm-state pm-state-${project.onboardingState ?? 'registered'}`}>
          {onboardingStateLabel(project.onboardingState ?? 'registered')}
        </span>
      </div>
      <ul className="pm-repos">
        {project.repos.map((r) => (
          <li key={r.id}>
            {r.repo}
            {r.isPrimary && <span className="pm-primary-chip">primary</span>}
            {!r.backfillDone && <span className="pm-backfill-chip">backfilling</span>}
          </li>
        ))}
      </ul>
      <div className="pm-counts">
        <span>{project.workItemCount} work items</span>
        <span>{project.featureCount} features</span>
      </div>
      {project.slug && <OnboardingWizard slug={project.slug} />}
      {project.slug && <DependencyPanel slug={project.slug} />}
      <div className="pm-card-actions">
        {sync.isError && (
          <p role="alert" className="pm-error">
            {extractMutationErrorMessage(sync.error, 'Could not request sync')}
          </p>
        )}
        <button
          onClick={() => project.slug && sync.mutate(project.slug)}
          disabled={sync.isPending || !project.slug}
        >
          {sync.isPending ? 'Requesting…' : 'Sync now'}
        </button>
      </div>
    </div>
  )
}

/**
 * Link to the Triage section, with the queue depth as the reason to follow it.
 *
 * Phase 2 embedded the whole plain triage review list here (deleted with this
 * change — see `triage/TriageInbox`). Phase 3 replaces it with
 * the keyboard-first inbox at `/triage` (design §8), which is cross-project and
 * so has no business living inside one product's registry page. The path comes
 * from `NAV_DEFS`, not a literal, so this cannot drift from the route table.
 */
function TriageCta() {
  const { data } = useTriage()
  const n = data?.length ?? 0
  return (
    <Link className="pm-triage-cta" to={NAV_BY_ID.triage.path}>
      {n === 0
        ? 'Open the Triage inbox →'
        : `Review ${n} proposal${n === 1 ? '' : 's'} in Triage →`}
    </Link>
  )
}

export function ProjectsTab() {
  const { data: projects, isLoading } = useProjects()
  const [showForm, setShowForm] = useState(false)
  return (
    <div className="flex flex-col h-full min-h-0" data-page-variant="standard">
      <PageHeader
        title={PRODUCT.plural}
        actions={<Button size="sm" onClick={() => setShowForm(true)}>{PRODUCT.new}</Button>}
      />
      <PageContent busy={isLoading} className="pm-projects">
        {showForm && <NewProjectForm onClose={() => setShowForm(false)} />}
        {!isLoading && (projects ?? []).length === 0 && (
          <p className="pm-muted">{PRODUCT.empty}</p>
        )}
        <div className="pm-project-grid">
          {(projects ?? []).map((p) => <ProjectCard key={p.id} project={p} />)}
        </div>
        <TriageCta />
      </PageContent>
    </div>
  )
}
