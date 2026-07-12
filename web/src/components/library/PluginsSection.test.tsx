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
          plugins: [
            {
              id: 'alpha@m',
              name: 'alpha',
              marketplace: 'm',
              enabled: true,
              version: '1.0.0',
              description: 'Alpha plugin',
              category: 'tools',
              components: { commands: 1, skills: 0, agents: 0, mcpServers: 0, hooks: 0 },
              tokenCost: { alwaysOn: 200, onInvoke: 100 },
            },
          ],
          marketplaces: [{ name: 'default' }],
        })
      }
      if (path === '/library/plugins/alpha%40m') {
        return Promise.resolve({
          id: 'alpha@m',
          name: 'alpha',
          marketplace: 'm',
          enabled: true,
          components: { commands: 1, skills: 0, agents: 0, mcpServers: 0, hooks: 0 },
          commands: [{ path: 'do/thing', description: 'Does a thing' }],
          skills: [],
          agents: [],
          mcpServers: [],
          hasHooks: false,
        })
      }
      return Promise.resolve({})
    }),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn().mockResolvedValue({ id: 'alpha@m', enabled: false }),
    del: vi.fn(),
  },
}))

import { PluginsSection } from './PluginsSection'
import { api } from '@/api/client'

function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <PluginsSection />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('PluginsSection', () => {
  it('renders plugin rows with name', async () => {
    renderSection()
    expect(await screen.findByText('alpha')).toBeTruthy()
  })

  it('shows plugin version, category and component summary', async () => {
    renderSection()
    expect(await screen.findByText('1.0.0')).toBeTruthy()
    expect(await screen.findByText('tools')).toBeTruthy()
    // 1 command → "1 commands" in the summary
    expect(await screen.findByText('1 commands')).toBeTruthy()
  })

  it('shows marketplaces when present', async () => {
    renderSection()
    expect(await screen.findByText(/Marketplaces:/)).toBeTruthy()
    expect(await screen.findByText(/default/)).toBeTruthy()
  })

  it('toggle switch calls PATCH with encoded id and new state', async () => {
    renderSection()
    const sw = await screen.findByRole('switch', { name: 'Enable alpha' })
    await userEvent.click(sw)
    expect(api.patch).toHaveBeenCalledWith('/library/plugins/alpha%40m', { enabled: false })
  })

  it('expanding a row fetches plugin detail and shows a command', async () => {
    renderSection()
    // The row button expands — click on the plugin name area
    const expandBtn = await screen.findByRole('button', { name: /alpha/i })
    await userEvent.click(expandBtn)
    // After expansion, the detail GET fires and the command path should appear
    expect(await screen.findByText('/do:thing')).toBeTruthy()
  })

  it('shows token cost badge when tokenCost is present', async () => {
    renderSection()
    // The alpha plugin has tokenCost: { alwaysOn: 200, onInvoke: 100 }
    expect(await screen.findByText('≈200 tok always-on')).toBeTruthy()
  })
})
