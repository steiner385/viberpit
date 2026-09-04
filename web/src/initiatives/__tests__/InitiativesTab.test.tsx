import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

// The board holds a live WebSocket; stub it so tests don't open a real socket.
vi.mock('@/ws/client', () => ({
  useSessionSocket: () => ({ on: vi.fn(), off: vi.fn() }),
}))

// Mock the fetchers but keep the real INITIATIVE_STAGES / types the board imports.
vi.mock('@/api/sdlc', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/sdlc')>()
  return {
    ...actual,
    fetchInitiatives: vi.fn(),
    advanceInitiative: vi.fn(),
    createInitiative: vi.fn(),
  }
})

import { InitiativesTab } from '../InitiativesTab'
import { ApiRequestError } from '@/api/client'
import * as sdlc from '@/api/sdlc'

const mocked = sdlc as unknown as {
  fetchInitiatives: ReturnType<typeof vi.fn>
  advanceInitiative: ReturnType<typeof vi.fn>
  createInitiative: ReturnType<typeof vi.fn>
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

const INIT: sdlc.Initiative = {
  slug: 'ai-native-sdlc', repo: 'steiner385/ccswitch', title: 'AI-native SDLC',
  stage: 'design', status: 'active', origin: 'human', originRef: null,
  epicRepo: null, epicNumber: null,
  createdAt: null, updatedAt: null,
  artifacts: [
    {
      id: 'a1', kind: 'intent', repoPath: 'docs/sdlc/x/intent.md', commitSha: 'abc1234',
      prNumber: 795, prUrl: 'https://github.com/steiner385/ccswitch/pull/795',
      sessionId: null, model: null, createdAt: null,
    },
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('InitiativesTab', () => {
  it('lists initiatives with their stage track and artifact link pinned at the SHA', async () => {
    mocked.fetchInitiatives.mockResolvedValue([INIT])
    render(<InitiativesTab />, { wrapper })

    await waitFor(() => expect(screen.getByText('AI-native SDLC')).toBeInTheDocument())
    // Progression track present (7 lifecycle nodes), current stage active.
    expect(document.querySelectorAll('.node')).toHaveLength(7)
    // Artifact links to its committed file at its commit SHA, not HEAD.
    const link = screen.getByRole('link', { name: /intent/i })
    expect(link).toHaveAttribute(
      'href', 'https://github.com/steiner385/ccswitch/blob/abc1234/docs/sdlc/x/intent.md')
  })

  it('passes the epic progress through to the Build node as a deep link', async () => {
    mocked.fetchInitiatives.mockResolvedValue([{
      ...INIT, stage: 'build', epicRepo: 'o/r', epicNumber: 997,
      epic: {
        ref: 'o/r#997', url: 'https://github.com/o/r/issues/997',
        total: 10, done: 3, blocked: 1, needsHuman: 0, children: [],
      },
    }])
    render(<InitiativesTab />, { wrapper })

    await waitFor(() => expect(screen.getByText('AI-native SDLC')).toBeInTheDocument())
    // The deep link lives inside an aria-hidden decorative node (the track's
    // container already exposes an aria-label summarizing all nodes), so query
    // the DOM directly rather than by role, matching the other track assertions.
    const link = document.querySelector('.node.active a.node-label')
    expect(link).toHaveTextContent('Build 3/10')
    expect(link).toHaveAttribute('href', 'https://github.com/o/r/issues/997')
  })

  it('shows an empty state when there are no initiatives', async () => {
    mocked.fetchInitiatives.mockResolvedValue([])
    render(<InitiativesTab />, { wrapper })
    await waitFor(() => expect(screen.getByText(/No initiatives yet/i)).toBeInTheDocument())
  })

  it('advances a gate via POST /advance', async () => {
    mocked.fetchInitiatives.mockResolvedValue([INIT])
    mocked.advanceInitiative.mockResolvedValue({ ...INIT, stage: 'plan' })
    render(<InitiativesTab />, { wrapper })

    const btn = await screen.findByRole('button', { name: /Advance/ })
    // design → plan is the next gate.
    expect(btn).toHaveTextContent('plan')
    fireEvent.click(btn)
    await waitFor(() =>
      expect(mocked.advanceInitiative).toHaveBeenCalledWith('ai-native-sdlc'))
  })

  it('renders the server error inline next to the Advance button when /advance fails', async () => {
    mocked.fetchInitiatives.mockResolvedValue([INIT])
    mocked.advanceInitiative.mockRejectedValue(
      new ApiRequestError(409, { detail: { message: 'nope' } }))
    render(<InitiativesTab />, { wrapper })

    fireEvent.click(await screen.findByRole('button', { name: /Advance/ }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('nope')
    expect(alert).toHaveClass('init-error')
  })

  it('renders the server error inline in the New initiative form when create fails', async () => {
    mocked.fetchInitiatives.mockResolvedValue([])
    mocked.createInitiative.mockRejectedValue(
      new ApiRequestError(409, { detail: { message: 'nope' } }))
    render(<InitiativesTab />, { wrapper })

    fireEvent.click(await screen.findByRole('button', { name: /New initiative/ }))
    fireEvent.change(screen.getByLabelText('slug'), { target: { value: 'x' } })
    fireEvent.change(screen.getByLabelText('title'), { target: { value: 'X' } })
    fireEvent.change(screen.getByLabelText('repo'), { target: { value: 'o/r' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('nope')
    // The form stays open so the user can fix and retry.
    expect(screen.getByLabelText('slug')).toBeInTheDocument()
  })
})
