/**
 * Products registry (PM product layer, phase 1): project cards + New Project
 * form + sync-now. Deliberately minimal — the onboarding wizard, board, and
 * inbox land in later phases (design 2026-08-31 §10).
 */
import { useState } from 'react'

import { useCreateProject, useProjects, useSyncProject } from '@/api/hooks'
import type { Project } from '@/api/pm'
import { extractMutationErrorMessage } from '@/api/queryClient'

import { DependencyPanel } from './DependencyPanel'
import { OnboardingWizard } from './OnboardingWizard'
import { TriageReviewList } from './TriageReviewList'
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
               placeholder="KinDash" />
      </label>
      <label>
        Primary repo
        <input value={repo} onChange={(e) => setRepo(e.target.value)}
               placeholder="owner/repo" />
      </label>
      {create.isError && (
        <p role="alert" className="pm-error">
          {extractMutationErrorMessage(create.error, 'Could not create project')}
        </p>
      )}
      <div className="pm-form-actions">
        <button type="button" onClick={onClose}>Cancel</button>
        <button type="submit" disabled={!canSubmit || create.isPending}>
          {create.isPending ? 'Creating…' : 'Add project'}
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
          {project.onboardingState ?? 'registered'}
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

export function ProjectsTab() {
  const { data: projects, isLoading } = useProjects()
  const [showForm, setShowForm] = useState(false)
  return (
    <div className="pm-projects">
      <div className="pm-projects-toolbar">
        <h2>Products</h2>
        <button onClick={() => setShowForm(true)}>New project</button>
      </div>
      {showForm && <NewProjectForm onClose={() => setShowForm(false)} />}
      {isLoading && <p className="pm-muted">Loading…</p>}
      {!isLoading && (projects ?? []).length === 0 && (
        <p className="pm-muted">
          No projects yet. Add one to start mirroring its backlog.
        </p>
      )}
      <div className="pm-project-grid">
        {(projects ?? []).map((p) => <ProjectCard key={p.id} project={p} />)}
      </div>
      <TriageReviewList />
    </div>
  )
}
