import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('@/api/client', () => ({
  ApiRequestError: class extends Error {},
  api: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn().mockResolvedValue({}),
    patch: vi.fn(),
    del: vi.fn().mockResolvedValue({}),
  },
}))

import { McpSection } from './McpSection'
import { api } from '@/api/client'

function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <McpSection />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const mcpResponse = {
  servers: [
    {
      name: 'my-server',
      source: 'user',
      readonly: false,
      transport: 'stdio' as const,
      config: { type: 'stdio', command: 'npx', args: ['-y', 'some-mcp'] },
      needsAuth: ['acct1', 'acct2'],
    },
    {
      name: 'plugin-server',
      source: 'alpha@m',
      readonly: true,
      transport: 'http' as const,
      config: { type: 'http', url: 'https://plugin.example/mcp' },
      needsAuth: [],
    },
    {
      name: 'httpurl-server',
      source: 'user',
      readonly: false,
      transport: 'http' as const,
      config: { type: 'http', httpUrl: 'https://via-httpurl.example/mcp' },
      needsAuth: [],
    },
    {
      name: 'extra-keys-server',
      source: 'user',
      readonly: false,
      transport: 'stdio' as const,
      config: { type: 'stdio', command: 'my-cmd', args: [], customKey: 'preserve-me' },
      needsAuth: [],
    },
  ],
}

beforeEach(() => {
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path === '/library/mcp') return Promise.resolve(mcpResponse)
    return Promise.resolve({})
  })
})

describe('McpSection', () => {
  // (a) user + plugin rows render with transport and needs-auth badges
  it('renders rows with transport badges and needs-auth badge', async () => {
    renderSection()
    // User server row
    expect(await screen.findByText('my-server')).toBeTruthy()
    // Transport badges (multiple stdio servers now, use findAllByText)
    expect((await screen.findAllByText('stdio')).length).toBeGreaterThan(0)
    expect((await screen.findAllByText('http')).length).toBeGreaterThan(0)
    // Plugin source badge
    expect(await screen.findByText('alpha@m')).toBeTruthy()
    // Needs-auth badge for my-server
    expect(await screen.findByText(/needs auth/i)).toBeTruthy()
    expect(await screen.findByText(/acct1/)).toBeTruthy()
  })

  // (b) Add server → fill name, transport http, url → Save calls api.put
  it('Add server: fill name, transport=http, url → Save calls api.put', async () => {
    renderSection()
    const addBtn = await screen.findByRole('button', { name: /add server/i })
    await userEvent.click(addBtn)

    // Dialog opens
    expect(await screen.findByRole('dialog')).toBeTruthy()

    // Fill name
    const nameInput = screen.getByLabelText(/server name/i)
    await userEvent.clear(nameInput)
    await userEvent.type(nameInput, 'srv')

    // Change transport to http
    const transportSelect = screen.getByRole('combobox', { name: /transport/i })
    await userEvent.selectOptions(transportSelect, 'http')

    // Fill url
    const urlInput = screen.getByRole('textbox', { name: /^url$/i })
    await userEvent.clear(urlInput)
    await userEvent.type(urlInput, 'https://x/mcp')

    // Click Save
    const saveBtn = screen.getByRole('button', { name: /^save$/i })
    await userEvent.click(saveBtn)

    await waitFor(() => {
      expect(api.put).toHaveBeenCalledWith('/library/mcp/srv', {
        config: { type: 'http', url: 'https://x/mcp' },
      })
    })
  })

  // (c) Delete flow calls api.del
  it('delete flow calls api.del', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderSection()

    // Click the user server row to open edit dialog
    const serverRow = await screen.findByRole('button', { name: /my-server/i })
    await userEvent.click(serverRow)

    // Dialog opens
    expect(await screen.findByRole('dialog')).toBeTruthy()

    // Click Delete
    const deleteBtn = await screen.findByRole('button', { name: /^delete$/i })
    await userEvent.click(deleteBtn)

    await waitFor(() => {
      expect(api.del).toHaveBeenCalledWith('/library/mcp/my-server')
    })
  })

  // (d) Plugin server dialog has no Save button
  it('plugin server dialog has no Save button', async () => {
    renderSection()

    const pluginRow = await screen.findByRole('button', { name: /plugin-server/i })
    await userEvent.click(pluginRow)

    // Dialog opens
    expect(await screen.findByRole('dialog')).toBeTruthy()

    // No Save button
    expect(screen.queryByRole('button', { name: /^save$/i })).toBeNull()
    // Cancel present
    expect(screen.getByRole('button', { name: /cancel/i })).toBeTruthy()
  })

  // (e) Editing server with httpUrl shows the url value in the URL input
  it('editing server with httpUrl shows httpUrl value in URL field', async () => {
    renderSection()

    const serverRow = await screen.findByRole('button', { name: /httpurl-server/i })
    await userEvent.click(serverRow)

    expect(await screen.findByRole('dialog')).toBeTruthy()

    // The URL input should show the httpUrl value (seeded from httpUrl fallback)
    const urlInput = screen.getByRole('textbox', { name: /^url$/i })
    expect((urlInput as HTMLInputElement).value).toBe('https://via-httpurl.example/mcp')
  })

  // (f) Entering JSON mode with a pristine edit form shows full server.config
  it('entering JSON mode with pristine edit form shows full server.config', async () => {
    renderSection()

    const serverRow = await screen.findByRole('button', { name: /my-server/i })
    await userEvent.click(serverRow)

    expect(await screen.findByRole('dialog')).toBeTruthy()

    // Without modifying anything, click "Edit as JSON"
    const jsonModeBtn = screen.getByRole('button', { name: /edit as json/i })
    await userEvent.click(jsonModeBtn)

    const jsonTextarea = screen.getByLabelText(/config json/i) as HTMLTextAreaElement
    // The full server.config should be serialized, not just the form-derived fields
    expect(jsonTextarea.value).toContain('"command"')
    expect(jsonTextarea.value).toContain('"npx"')
    expect(jsonTextarea.value).toContain('"args"')
  })

  // (g) Saving in form mode preserves extra config keys
  it('saving in form mode preserves extra config keys', async () => {
    renderSection()

    const serverRow = await screen.findByRole('button', { name: /extra-keys-server/i })
    await userEvent.click(serverRow)

    expect(await screen.findByRole('dialog')).toBeTruthy()

    // Click Save without changing anything
    const saveBtn = screen.getByRole('button', { name: /^save$/i })
    await userEvent.click(saveBtn)

    await waitFor(() => {
      expect(api.put).toHaveBeenCalledWith(
        '/library/mcp/extra-keys-server',
        expect.objectContaining({
          config: expect.objectContaining({ customKey: 'preserve-me' }),
        }),
      )
    })
  })
})
