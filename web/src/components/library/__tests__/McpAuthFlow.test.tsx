import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as React from 'react'

// Mock hooks before importing the component
vi.mock('@/api/hooks', () => ({
  useStartMcpAuth: vi.fn(),
  useMcpAuthStatus: vi.fn(),
  useSubmitMcpRedirect: vi.fn(),
  useCancelMcpAuth: vi.fn(),
}))

import { McpAuthFlow } from '../McpAuthFlow'
import {
  useStartMcpAuth,
  useMcpAuthStatus,
  useSubmitMcpRedirect,
  useCancelMcpAuth,
} from '@/api/hooks'
import { idleQueryResult, mutationResult, queryResult } from '@/test/reactQuery'

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: qc }, children)
}

const noop = vi.fn()
const mutate = vi.fn()

function mockIdleHooks() {
  vi.mocked(useStartMcpAuth).mockReturnValue(mutationResult({ mutate }))
  vi.mocked(useMcpAuthStatus).mockReturnValue(idleQueryResult())
  vi.mocked(useSubmitMcpRedirect).mockReturnValue(mutationResult({ mutate: noop }))
  vi.mocked(useCancelMcpAuth).mockReturnValue(mutationResult({ mutate: noop }))
}

beforeEach(() => {
  vi.clearAllMocks()
  mockIdleHooks()
})

describe('McpAuthFlow', () => {
  // (a) awaiting_redirect state renders the auth URL + a paste input
  it('shows the auth URL and a paste box when awaiting redirect', () => {
    vi.mocked(useMcpAuthStatus).mockReturnValue(queryResult({
        state: 'awaiting_redirect',
        server: 'notion',
        message: null,
        authUrl: 'https://api.notion.com/v1/oauth/authorize?client_id=abc',
        accounts: [],
        startedAt: 123,
      }))

    render(
      <McpAuthFlow server="notion" accounts={['acct1']} />,
      { wrapper: makeWrapper() },
    )

    // Auth URL visible as a link
    expect(screen.getByText(/https:\/\/api\.notion\.com/)).toBeTruthy()
    // Paste redirect URL input
    expect(screen.getByPlaceholderText(/redirect url/i)).toBeTruthy()
    // Submit button
    expect(screen.getByRole('button', { name: /submit/i })).toBeTruthy()
    // Cancel button
    expect(screen.getByRole('button', { name: /cancel/i })).toBeTruthy()
  })

  // (b) success state renders "cleared for N account(s)"
  it('shows "cleared for N account(s)" on success', () => {
    vi.mocked(useMcpAuthStatus).mockReturnValue(queryResult({
        state: 'success',
        server: 'notion',
        message: null,
        authUrl: null,
        accounts: ['acct1', 'acct2'],
        startedAt: 123,
      }))

    render(
      <McpAuthFlow server="notion" accounts={['acct1', 'acct2']} />,
      { wrapper: makeWrapper() },
    )

    expect(screen.getByText(/cleared for 2 account\(s\)/i)).toBeTruthy()
  })

  // (c) error state renders the verbatim status.message
  it('shows the verbatim message on error', () => {
    vi.mocked(useMcpAuthStatus).mockReturnValue(queryResult({
        state: 'error',
        server: 'notion',
        message: 'OAuth provider returned 401 — check credentials.',
        authUrl: null,
        accounts: [],
        startedAt: 123,
      }))

    render(
      <McpAuthFlow server="notion" accounts={['acct1']} />,
      { wrapper: makeWrapper() },
    )

    expect(screen.getByText('OAuth provider returned 401 — check credentials.')).toBeTruthy()
    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
  })

  // (d) idle state renders the "Fix auth" button
  it('shows Fix auth button when idle', () => {
    render(
      <McpAuthFlow server="notion" accounts={['acct1']} />,
      { wrapper: makeWrapper() },
    )

    expect(screen.getByRole('button', { name: /fix auth/i })).toBeTruthy()
  })

  // (f) awaiting_redirect with no authUrl shows the unavailable fallback
  it('shows unavailable fallback when awaiting_redirect and authUrl is null', () => {
    vi.mocked(useMcpAuthStatus).mockReturnValue(queryResult({
        state: 'awaiting_redirect',
        server: 'notion',
        message: null,
        authUrl: null,
        accounts: [],
        startedAt: 123,
      }))

    render(
      <McpAuthFlow server="notion" accounts={['acct1']} />,
      { wrapper: makeWrapper() },
    )

    expect(screen.getByText(/authorization link unavailable/i)).toBeTruthy()
    // Paste input still renders so the user can still submit if they have the URL
    expect(screen.getByPlaceholderText(/redirect url/i)).toBeTruthy()
  })

  // (g) error state with null message shows the fallback "Authentication failed."
  it('shows fallback text when error state has no message', () => {
    vi.mocked(useMcpAuthStatus).mockReturnValue(queryResult({
        state: 'error',
        server: 'notion',
        message: null,
        authUrl: null,
        accounts: [],
        startedAt: 123,
      }))

    render(
      <McpAuthFlow server="notion" accounts={['acct1']} />,
      { wrapper: makeWrapper() },
    )

    expect(screen.getByRole('alert')).toBeTruthy()
    expect(screen.getByText('Authentication failed.')).toBeTruthy()
    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
  })

  // (e) clicking Fix auth calls startMcpAuth.mutate
  it('clicking Fix auth calls mutate with the server name', async () => {
    const startMutate = vi.fn()
    vi.mocked(useStartMcpAuth).mockReturnValue(mutationResult({ mutate: startMutate }))

    render(
      <McpAuthFlow server="notion" accounts={['acct1']} />,
      { wrapper: makeWrapper() },
    )

    const btn = screen.getByRole('button', { name: /fix auth/i })
    await userEvent.click(btn)
    expect(startMutate).toHaveBeenCalledWith('notion')
  })
})
