// "Adopt a document" on an initiative (#2160): register a spec/plan/intent that
// already exists in the repo (spec-kit, a brainstorm design, an epic body) as the
// stage artifact, instead of the synthesizer writing docs/sdlc/<slug>/<kind>.md.
// Only the kinds the initiative does not have yet are offered — the server keeps
// one artifact per kind and refuses a second (409).
import { useRef, useState } from 'react'
import { INITIATIVE_ARTIFACT_KINDS, type ArtifactKind, type Initiative } from '@/api/sdlc'
import { useAdoptArtifact } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Dialog } from '@/ui/Dialog'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Select } from '@/ui/Select'
import { Button } from '@/ui/Button'

export function AdoptArtifactDialog({ initiative, open, onClose }: {
  initiative: Initiative
  open: boolean
  onClose: () => void
}) {
  const adopt = useAdoptArtifact()
  const pathRef = useRef<HTMLInputElement>(null)
  const taken = new Set(initiative.artifacts.map((a) => a.kind))
  const available = INITIATIVE_ARTIFACT_KINDS.filter((k) => !taken.has(k))

  const [kind, setKind] = useState<ArtifactKind>(available[0] ?? 'spec')
  const [repoPath, setRepoPath] = useState('')
  const [commitSha, setCommitSha] = useState('')

  const path = repoPath.trim()
  const sha = commitSha.trim()
  const canSave = available.length > 0 && path !== '' && sha !== '' && !adopt.isPending

  const save = () => {
    adopt.mutate({ slug: initiative.slug, input: { kind, repoPath: path, commitSha: sha } },
      { onSuccess: onClose })
  }

  return (
    <Dialog open={open} onClose={onClose} title="Adopt an existing document" size="md" initialFocus={pathRef}>
      <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); if (canSave) save() }}>
        <p className="text-sm text-ink-muted">
          Record a document that is already committed as this initiative&apos;s artifact. No session is
          started — the stage treats it as merged.
        </p>
        <Field label="Artifact" htmlFor="aa-kind">
          <Select id="aa-kind" value={kind} onChange={(e) => setKind(e.target.value as ArtifactKind)}>
            {available.map((k) => <option key={k} value={k}>{k}</option>)}
          </Select>
        </Field>
        <Field label="Path in the repo" htmlFor="aa-path" help="e.g. specs/007-shared-task-model/spec.md" required>
          <Input ref={pathRef} id="aa-path" value={repoPath} onChange={(e) => setRepoPath(e.target.value)} />
        </Field>
        <Field label="Commit" htmlFor="aa-sha" help="The commit on the default branch that contains it" required>
          <Input id="aa-sha" value={commitSha} onChange={(e) => setCommitSha(e.target.value)} />
        </Field>
        {available.length === 0 && (
          <p className="text-sm text-ink-muted">Every artifact kind is already recorded.</p>
        )}
        {adopt.isError && (
          <p role="alert" className="text-sm text-danger">
            {extractMutationErrorMessage(adopt.error, 'Could not adopt the document.')}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" size="lg" disabled={!canSave} loading={adopt.isPending}>Adopt</Button>
        </div>
      </form>
    </Dialog>
  )
}
