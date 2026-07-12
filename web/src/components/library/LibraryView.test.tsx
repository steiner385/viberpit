import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('@/api/client', () => ({
  ApiRequestError: class extends Error {},
  api: {
    get: vi.fn().mockImplementation((path: string) => {
      if (path === '/library/plugins') {
        return Promise.resolve({
          plugins: [{ id: 'alpha@m', name: 'alpha', marketplace: 'm', enabled: true, description: 'Alpha plugin', components: { commands: 1, skills: 0, agents: 0, mcpServers: 0, hooks: 0 } }],
          marketplaces: [],
        })
      }
      if (path === '/library/skills') return Promise.resolve({ skills: [{ name: 'my-skill', source: 'user', readonly: false }] })
      if (path === '/library/commands') return Promise.resolve({ commands: [] })
      if (path === '/library/mcp') return Promise.resolve({ servers: [] })
      return Promise.resolve({})
    }),
    post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn(),
  },
}))

import { LibraryView } from './LibraryView'

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/library']}>
        <LibraryView />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('LibraryView', () => {
  it('defaults to the Plugins section and lists plugins', async () => {
    renderView()
    expect(await screen.findByText('alpha')).toBeTruthy()
  })

  it('switches sections', async () => {
    renderView()
    await userEvent.click(screen.getByRole('tab', { name: /skills/i }))
    expect(await screen.findByText('my-skill')).toBeTruthy()
  })
})
