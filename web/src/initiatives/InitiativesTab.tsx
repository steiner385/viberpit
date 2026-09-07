// The Initiative board (issue #803): every SDLC initiative rendered as a
// horizontal artifact/stage track, each committed artifact linking to its file
// *at its SHA*, an "Advance" button per gate tapping POST /advance. Reuses the
// pipeline metro idiom (InitiativeTrack) and design tokens (.prdash-root).
import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useInitiatives, useAdvanceInitiative, useCreateInitiative, QK } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { useSessionSocket } from '@/ws/client'
import { INITIATIVE_STAGES, type Initiative, type InitiativeArtifact } from '@/api/sdlc'
import { fileUrl, commitUrl, prUrl } from '@/lib/githubUrl'
import { InitiativeTrack } from './InitiativeTrack'
import { IncidentQueue } from './IncidentQueue'
import '../pipeline/plotroom.css'
import './initiatives.css'

/** Live nudge: the artifact chain moves when SDLC-spawned sessions change state,
 *  so invalidate the board on `/ws` status frames (the same socket the rest of
 *  the app already holds open) rather than polling hard. Baseline polling in
 *  useInitiatives covers the gaps.
 *
 *  Incidents ride the same nudge: a 2σ breach spawns a diagnosis session, and
 *  SDLC Intake adopting a queued incident spawns another — both of which move a
 *  row in the queue and both of which arrive as status frames. */
function useInitiativesLive() {
  const qc = useQueryClient()
  const { on, off } = useSessionSocket()
  useEffect(() => {
    const nudge = () => {
      void qc.invalidateQueries({ queryKey: QK.initiatives })
      void qc.invalidateQueries({ queryKey: QK.incidents })
    }
    on('status', nudge)
    return () => off('status', nudge)
  }, [on, off, qc])
}

const STATUS_LABEL: Record<string, string> = {
  proposed: 'Proposed', active: 'Active', awaiting_human: 'Awaiting human',
  blocked: 'Blocked', done: 'Done',
}

function nextStage(stage: string | null): string | null {
  const i = INITIATIVE_STAGES.indexOf(stage as (typeof INITIATIVE_STAGES)[number])
  if (i < 0 || i + 1 >= INITIATIVE_STAGES.length) return null
  return INITIATIVE_STAGES[i + 1]!
}

function ArtifactLink({ repo, artifact }: { repo: string | null; artifact: InitiativeArtifact }) {
  const kind = artifact.kind ?? 'artifact'
  // Prefer the file pinned at its commit; fall back to the PR, then bare text.
  if (repo && artifact.repoPath && artifact.commitSha) {
    return (
      <a className="workspace-link" target="_blank" rel="noreferrer"
         href={fileUrl(repo, artifact.commitSha, artifact.repoPath)}
         title={`${artifact.repoPath} @ ${artifact.commitSha.slice(0, 7)}`}>
        {kind} ↗
      </a>
    )
  }
  if (artifact.prUrl || (repo && artifact.prNumber)) {
    const href = artifact.prUrl ?? prUrl(repo!, artifact.prNumber!)
    return (
      <a className="workspace-link" target="_blank" rel="noreferrer" href={href}>
        {kind} (PR #{artifact.prNumber}) ↗
      </a>
    )
  }
  return <span className="init-artifact-pending" title="not committed yet">{kind} (pending)</span>
}

function InitiativeRow({ init }: { init: Initiative }) {
  const advance = useAdvanceInitiative()
  const nxt = nextStage(init.stage)
  return (
    <section className="init-card" aria-label={init.title ?? init.slug}>
      <header className="init-card-head">
        <div>
          <h3 className="init-title">{init.title ?? init.slug}</h3>
          <div className="init-meta">
            <span className="init-slug">{init.slug}</span>
            {init.repo && <span className="init-repo">{init.repo}</span>}
            <span className={`init-status init-status-${init.status}`}>
              {STATUS_LABEL[init.status ?? ''] ?? init.status}
            </span>
          </div>
        </div>
        <button
          type="button"
          className="btn-ghost init-advance"
          disabled={!nxt || advance.isPending}
          onClick={() => advance.mutate(init.slug)}
          title={nxt ? `Advance the gate to “${nxt}”` : 'Already at the final stage'}
        >
          {nxt ? `Advance → ${nxt}` : 'Complete'}
        </button>
      </header>
      {advance.isError && (
        <p role="alert" className="init-error">
          {extractMutationErrorMessage(advance.error, 'Could not advance initiative')}
        </p>
      )}
      <InitiativeTrack stage={init.stage} status={init.status} epic={init.epic} />
      <div className="init-artifacts">
        {init.artifacts.length === 0
          ? <span className="init-artifact-pending">no artifacts committed yet</span>
          : init.artifacts.map((a) => <ArtifactLink key={a.id} repo={init.repo} artifact={a} />)}
        {init.repo && init.artifacts.some((a) => a.commitSha) && (
          <a className="workspace-link init-commit-link" target="_blank" rel="noreferrer"
             href={commitUrl(init.repo, init.artifacts.find((a) => a.commitSha)!.commitSha!)}>
            latest commit ↗
          </a>
        )}
      </div>
    </section>
  )
}

function NewInitiativeForm() {
  const create = useCreateInitiative()
  const [open, setOpen] = useState(false)
  const [slug, setSlug] = useState('')
  const [title, setTitle] = useState('')
  const [repo, setRepo] = useState('')
  // repo is required — the backend's create body takes `repo: str` (no default),
  // so a blank one would 422; gate submission on it rather than surprise the user.
  const canSubmit =
    slug.trim().length > 0 && title.trim().length > 0 && repo.trim().length > 0 && !create.isPending
  if (!open) {
    return (
      <button type="button" className="btn-ghost" onClick={() => setOpen(true)}>
        + New initiative
      </button>
    )
  }
  return (
    <form
      className="init-new-form"
      onSubmit={(e) => {
        e.preventDefault()
        if (!canSubmit) return
        create.mutate(
          { slug: slug.trim(), title: title.trim(), repo: repo.trim(), origin: 'human' },
          { onSuccess: () => { setSlug(''); setTitle(''); setRepo(''); setOpen(false) } },
        )
      }}
    >
      <input aria-label="slug" placeholder="slug (e.g. ai-native-sdlc)"
             value={slug} onChange={(e) => setSlug(e.target.value)} />
      <input aria-label="title" placeholder="title"
             value={title} onChange={(e) => setTitle(e.target.value)} />
      <input aria-label="repo" placeholder="owner/repo"
             value={repo} onChange={(e) => setRepo(e.target.value)} />
      <button type="submit" className="btn-ghost" disabled={!canSubmit}>Create</button>
      <button type="button" className="btn-ghost" onClick={() => setOpen(false)}>Cancel</button>
      {create.isError && (
        <p role="alert" className="init-error">
          {extractMutationErrorMessage(create.error, 'Could not create initiative')}
        </p>
      )}
    </form>
  )
}

export function InitiativesTab() {
  useInitiativesLive()
  const { data: initiatives, isLoading, isError } = useInitiatives()
  return (
    <div className="prdash-root init-board flex-1 min-h-0 overflow-y-auto">
      <div className="init-board-head">
        <h2 className="init-board-title">Initiatives</h2>
        <NewInitiativeForm />
      </div>
      {/* The back of the SDLC loop sits above its front: an open incident is the
          thing most likely to become the next initiative on this board. */}
      <IncidentQueue />
      {isLoading && <p role="status">Loading initiatives…</p>}
      {isError && <p className="stale" role="status">Could not load initiatives.</p>}
      {!isLoading && !isError && (initiatives?.length ?? 0) === 0 && (
        <p className="init-empty">
          No initiatives yet. Create one, or let the SDLC automations open the first intent.
        </p>
      )}
      {initiatives?.map((init) => <InitiativeRow key={init.slug} init={init} />)}
    </div>
  )
}
