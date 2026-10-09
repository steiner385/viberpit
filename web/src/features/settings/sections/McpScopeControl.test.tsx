import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('@/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/client')>()),
  api: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn().mockResolvedValue({}),
    patch: vi.fn(),
    del: vi.fn().mockResolvedValue({}),
  },
}))

import { McpScopeControl } from './McpScopeControl'
import { api, ApiRequestError } from '@/api/client'

const KEY = 'plugin:Notion:notion'
const repo = (nameWithOwner: string) => ({
  nameWithOwner, name: nameWithOwner.split('/')[1], owner: nameWithOwner.split('/')[0],
  isPrivate: false, url: '', defaultBranch: 'main', pushedAt: null, cloned: false,
})

function mockGets(scope: unknown) {
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path === '/library/mcp/scope') {
      return scope instanceof Error ? Promise.reject(scope) : Promise.resolve(scope)
    }
    if (path === '/github/repos') return Promise.resolve([repo('Acme/Web'), repo('acme/api'), repo('acme/docs')])
    return Promise.resolve({})
  })
}

function renderControl() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <McpScopeControl serverKey={KEY} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.put).mockResolvedValue({})
  vi.mocked(api.del).mockResolvedValue({})
  mockGets({
    servers: { [KEY]: { defaultEnabled: true } },
    rules: [
      { server: KEY, repo: 'acme/web', enabled: false },
      { server: 'other', repo: 'acme/api', enabled: false },
    ],
  })
})

describe('McpScopeControl', () => {
  it('shows the current default, the hint, and only this server\'s exceptions', async () => {
    renderControl()
    expect(await screen.findByRole('radio', { name: 'On for all repos' })).toHaveProperty('checked', true)
    expect(screen.getByRole('radio', { name: 'Off by default' })).toHaveProperty('checked', false)
    expect(screen.getByText('Applies to new and resumed sessions.')).toBeTruthy()
    expect(screen.getByText(/acme\/web — off/)).toBeTruthy()
    expect(screen.queryByText(/acme\/api — off/)).toBeNull()
  })

  it('changing the default calls the set-default mutation', async () => {
    renderControl()
    await userEvent.click(await screen.findByRole('radio', { name: 'Off by default' }))
    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith(`/library/mcp/scope/${encodeURIComponent(KEY)}`, { defaultEnabled: false }))
  })

  it('adding an exception calls the set-rule mutation', async () => {
    renderControl()
    const picker = await screen.findByLabelText('Add exception')
    await screen.findByRole('option', { name: 'acme/api' })
    await userEvent.selectOptions(picker, 'acme/api')
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith(`/library/mcp/scope/${encodeURIComponent(KEY)}/repos/acme/api`, { enabled: false }))
  })

  it('the exception takes the opposite of the default', async () => {
    mockGets({ servers: { [KEY]: { defaultEnabled: false } }, rules: [] })
    renderControl()
    const picker = await screen.findByLabelText('Add exception')
    await screen.findByRole('option', { name: 'acme/api' })
    await userEvent.selectOptions(picker, 'acme/api')
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith(expect.stringContaining('/repos/acme/api'), { enabled: true }))
  })

  it('an explicit state choice overrides the opposite-of-default', async () => {
    renderControl()
    const picker = await screen.findByLabelText('Add exception')
    await screen.findByRole('option', { name: 'acme/api' })
    await userEvent.selectOptions(picker, 'acme/api')
    await userEvent.selectOptions(screen.getByLabelText('Exception state'), 'on')
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith(expect.stringContaining('/repos/acme/api'), { enabled: true }))
  })

  it('offers only github repos that have no exception yet (case-insensitive)', async () => {
    renderControl()
    await screen.findByRole('option', { name: 'acme/api' })
    const names = within(screen.getByLabelText('Add exception')).getAllByRole('option').map((o) => o.textContent)
    expect(names).toEqual(['Select a repo…', 'acme/api', 'acme/docs'])
  })

  it('removing an exception calls the delete mutation', async () => {
    renderControl()
    await userEvent.click(await screen.findByRole('button', { name: 'Remove exception acme/web' }))
    await waitFor(() =>
      expect(api.del).toHaveBeenCalledWith(`/library/mcp/scope/${encodeURIComponent(KEY)}/repos/acme/web`))
  })

  it('renders nothing when the feature is switched off (404 mcp_scope_disabled)', async () => {
    mockGets(new ApiRequestError(404, { error: 'mcp_scope_disabled' }))
    const { container } = renderControl()
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/library/mcp/scope'))
    await waitFor(() => expect(container.textContent).toBe(''))
  })

  it('surfaces a mutation error', async () => {
    vi.mocked(api.put).mockRejectedValue(new ApiRequestError(400, { detail: { message: 'unknown repo' } }))
    renderControl()
    await userEvent.click(await screen.findByRole('radio', { name: 'Off by default' }))
    expect((await screen.findByRole('alert')).textContent).toContain('unknown repo')
  })
})
