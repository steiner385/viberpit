// Recording a document the repo already has (spec-kit, a brainstorm design) as
// an initiative's stage artifact, without a synthesizer session (#2160).
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { Initiative } from '@/api/sdlc'
import { AdoptArtifactDialog } from './AdoptArtifactDialog'

const h = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false, isError: false }))
vi.mock('@/api/hooks', () => ({
  useAdoptArtifact: () => ({ mutate: h.mutate, isPending: h.isPending, isError: h.isError, error: null }),
}))

const initiative = {
  slug: 'shared-task-model', title: 'Shared task model', repo: 'o/r', stage: 'intake', status: 'proposed',
  origin: 'human', originRef: null, epicRepo: null, epicNumber: null, createdAt: null, updatedAt: null,
  artifacts: [], epic: null, projectSlug: 'kindash', primaryCapability: null, capabilities: [],
} as unknown as Initiative

describe('AdoptArtifactDialog', () => {
  beforeEach(() => { h.mutate.mockReset(); h.isPending = false; h.isError = false })

  it('offers every kind and disables Adopt until path and sha are filled', () => {
    render(<AdoptArtifactDialog initiative={initiative} open onClose={() => {}} />)
    expect(screen.getByLabelText('Artifact')).toHaveValue('intent')
    expect(['intent', 'spec', 'plan']).toEqual(
      screen.getAllByRole('option').map((o) => (o as HTMLOptionElement).value))
    expect(screen.getByRole('button', { name: 'Adopt' })).toBeDisabled()
  })

  it('sends the chosen kind, path and sha', () => {
    render(<AdoptArtifactDialog initiative={initiative} open onClose={() => {}} />)
    fireEvent.change(screen.getByLabelText('Artifact'), { target: { value: 'spec' } })
    fireEvent.change(screen.getByLabelText('Path in the repo', { exact: false }),
      { target: { value: 'specs/007-shared-task-model/spec.md' } })
    fireEvent.change(screen.getByLabelText('Commit', { exact: false }), { target: { value: 'deadbeef' } })
    fireEvent.click(screen.getByRole('button', { name: 'Adopt' }))
    expect(h.mutate).toHaveBeenCalledWith(
      {
        slug: 'shared-task-model',
        input: { kind: 'spec', repoPath: 'specs/007-shared-task-model/spec.md', commitSha: 'deadbeef' },
      },
      expect.anything(),
    )
  })

  it('only offers kinds the initiative does not already have', () => {
    const partial = { ...initiative, artifacts: [{ id: 'a1', kind: 'intent' }] } as unknown as Initiative
    render(<AdoptArtifactDialog initiative={partial} open onClose={() => {}} />)
    expect(['spec', 'plan']).toEqual(
      screen.getAllByRole('option').map((o) => (o as HTMLOptionElement).value))
  })

  it('says so and disables Adopt once every kind is recorded', () => {
    const full = {
      ...initiative,
      artifacts: [{ id: 'a1', kind: 'intent' }, { id: 'a2', kind: 'spec' }, { id: 'a3', kind: 'plan' }],
    } as unknown as Initiative
    render(<AdoptArtifactDialog initiative={full} open onClose={() => {}} />)
    expect(screen.getByText('Every artifact kind is already recorded.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Adopt' })).toBeDisabled()
  })
})
