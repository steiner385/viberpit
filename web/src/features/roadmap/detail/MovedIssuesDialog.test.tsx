// The moved-issue map dialog: it lists the stored pairs with their status,
// offers title-match suggestions as UNticked checkboxes (never auto-applied),
// takes manual pairs, and posts exactly the selected + manual pairs.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import type { Project, ProjectRepoInfo } from '@/api/pm'
import { MovedIssuesDialog } from './MovedIssuesDialog'

const state = vi.hoisted(() => ({
  pairs: [] as unknown[],
  suggestions: [] as unknown[],
  suggestArgs: [] as string[][],
  record: { mutate: vi.fn(), isPending: false, isError: false, error: null as unknown },
}))

vi.mock('@/api/hooks', () => ({
  useMovedRefs: () => ({ data: { pairs: state.pairs }, isLoading: false, isError: false }),
  useSuggestMovedRefs: (slug: string, from: string, to: string) => {
    state.suggestArgs.push([slug, from, to])
    return { data: { pairs: state.suggestions }, isLoading: false, isError: false }
  },
  useRecordMovedRefs: () => state.record,
}))

const repo = (r: string, extra: Partial<ProjectRepoInfo> = {}): ProjectRepoInfo => ({
  id: r, repo: r, isPrimary: false, backfillDone: true, lastSyncedAt: null,
  retiredAt: null, retiredReason: null, supersededBy: null, ...extra,
})

const PROJECT: Project = {
  id: 'p', slug: 'ghostdriver', name: 'Ghostdriver', description: null, status: null,
  portfolioTags: [], onboardingState: 'live', createdAt: null, updatedAt: null,
  repos: [
    repo('s/ghostdriver', { isPrimary: true }),
    repo('s/ghostdriver-mcp', { retiredAt: '2026-11-01T00:00:00Z', supersededBy: 's/ghostdriver' }),
  ],
  edges: [], featureCount: 0, workItemCount: 0, automationPolicy: null,
  automationPolicyUpdatedAt: null, priorityRank: null,
}

beforeEach(() => {
  state.pairs = []
  state.suggestions = []
  state.suggestArgs = []
  state.record = { mutate: vi.fn(), isPending: false, isError: false, error: null }
})

const mount = () => render(<MovedIssuesDialog project={PROJECT} open onClose={vi.fn()} />)

describe('MovedIssuesDialog', () => {
  it('lists the stored pairs with their status', () => {
    state.pairs = [
      { old: 's/ghostdriver-mcp#257', new: 's/ghostdriver#3', status: 'migrated', migratedAt: null, migration: null },
      { old: 's/ghostdriver-mcp#258', new: 's/ghostdriver#9', status: 'pending', migratedAt: null, migration: null },
    ]
    mount()
    const list = screen.getByRole('list', { name: 'Moved issues' })
    const items = within(list).getAllByRole('listitem')
    expect(items[0]).toHaveTextContent('s/ghostdriver-mcp#257 → s/ghostdriver#3')
    expect(items[0]).toHaveTextContent('migrated')
    expect(items[1]).toHaveTextContent('pending')
  })

  it('suggests from the retired repo to the primary by default', () => {
    mount()
    expect(state.suggestArgs.at(-1)).toEqual(['ghostdriver', 's/ghostdriver-mcp', 's/ghostdriver'])
  })

  it('posts only the ticked suggestions plus the manual pairs', () => {
    state.suggestions = [
      { old: 's/ghostdriver-mcp#1', new: 's/ghostdriver#11', title: 'Crash on start' },
      { old: 's/ghostdriver-mcp#2', new: 's/ghostdriver#12', title: 'Slow sync' },
    ]
    mount()
    const apply = screen.getByRole('button', { name: 'Apply' })
    // Suggestions are never pre-selected: nothing to apply yet.
    expect(apply).toBeDisabled()
    fireEvent.click(screen.getByLabelText(/s\/ghostdriver-mcp#2 → s\/ghostdriver#12/))
    fireEvent.change(screen.getByLabelText(/Old issue/), { target: { value: 's/ghostdriver-mcp#7' } })
    fireEvent.change(screen.getByLabelText(/New issue/), { target: { value: 's/ghostdriver#70' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add pair' }))
    expect(apply).toBeEnabled()
    fireEvent.click(apply)
    expect(state.record.mutate.mock.calls[0]![0]).toEqual([
      { old: 's/ghostdriver-mcp#2', new: 's/ghostdriver#12' },
      { old: 's/ghostdriver-mcp#7', new: 's/ghostdriver#70' },
    ])
  })

  it('refuses a manual pair that is not two owner/repo#N refs', () => {
    mount()
    fireEvent.change(screen.getByLabelText(/Old issue/), { target: { value: 'ghostdriver-mcp#7' } })
    fireEvent.change(screen.getByLabelText(/New issue/), { target: { value: 's/ghostdriver#70' } })
    expect(screen.getByRole('button', { name: 'Add pair' })).toBeDisabled()
  })

  it('shows the server message when the pairs are refused', async () => {
    const { ApiRequestError } = await import('@/api/client')
    state.record.isError = true
    state.record.error = new ApiRequestError(422, {
      detail: { error: 'bad_pairs', message: 's/a#1 -> s/b#2: chains', pairs: [] },
    })
    mount()
    expect(screen.getByRole('alert')).toHaveTextContent('s/a#1 -> s/b#2: chains')
  })
})
