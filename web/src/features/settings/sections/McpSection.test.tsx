import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
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

import { McpSection } from './McpSection'
import { api, ApiRequestError } from '@/api/client'

// Scope feature switched off: McpScopeControl renders nothing, so these specs stay about the rows.
function otherGet(path: string) {
  if (path === '/library/mcp/scope') return Promise.reject(new ApiRequestError(404, { error: 'mcp_scope_disabled' }))
  if (path === '/github/repos') return Promise.resolve([])
  return Promise.resolve({})
}

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
      status: ['acct1', 'acct2'].map((account) => ({
        account, host: 'dobby', server: 'my-server', status: 'needs-auth', error: null, toolCount: null,
        scope: 'user', source: 'probe' as const, sessionId: null, observedAt: '2026-10-09T12:00:00Z',
      })),
    },
    {
      name: 'plugin-server',
      source: 'alpha@m',
      readonly: true,
      transport: 'http' as const,
      config: { type: 'http', url: 'https://plugin.example/mcp' },
      status: [],
    },
    {
      name: 'httpurl-server',
      source: 'user',
      readonly: false,
      transport: 'http' as const,
      config: { type: 'http', httpUrl: 'https://via-httpurl.example/mcp' },
      status: [],
    },
    {
      name: 'extra-keys-server',
      source: 'user',
      readonly: false,
      transport: 'stdio' as const,
      config: { type: 'stdio', command: 'my-cmd', args: [], customKey: 'preserve-me' },
      status: [],
    },
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.post).mockReset()
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path === '/library/mcp') return Promise.resolve(mcpResponse)
    return otherGet(path)
  })
})

function row(over: Record<string, unknown> = {}) {
  return {
    account: 'a', host: 'dobby', server: 'x', status: 'connected', error: null, toolCount: null,
    scope: 'user', source: 'probe' as const, sessionId: null, observedAt: '2026-10-09T12:00:00Z', ...over,
  }
}

function mockServers(servers: unknown[]) {
  vi.mocked(api.get).mockImplementation((path: string) =>
    path === '/library/mcp' ? Promise.resolve({ servers }) : otherGet(path))
}

const srv = (name: string, transport: string, status: unknown[]) => ({
  name, source: 'user', readonly: false, transport, config: { type: transport }, status,
})

describe('McpSection', () => {
  it('shows the worst status pill and per-account detail', async () => {
    mockServers([srv('notion', 'http', [row({ account: 'a' }), row({ account: 'b', status: 'needs-auth', error: 'token expired' })])])
    renderSection()
    expect(await screen.findByText('Needs auth')).toBeTruthy()
    const toggle = screen.getByRole('button', { name: /details for notion/i })
    await userEvent.click(toggle)
    expect(document.getElementById(toggle.getAttribute('aria-controls')!)).toBeTruthy()
    expect(screen.getByText(/b · dobby/)).toBeTruthy()
    expect(screen.getByText('token expired')).toBeTruthy()
    expect(screen.getAllByText(/from probe/)).toHaveLength(2)
  })

  it('shows Not checked yet without rows and runs Test for that server', async () => {
    vi.mocked(api.post).mockResolvedValue({ rows: [], skipped: [{ account: 'z', reason: 'host offline' }] })
    mockServers([srv('gh', 'stdio', [])])
    renderSection()
    expect(await screen.findByText('Not checked yet')).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Test gh' }))
    expect(api.post).toHaveBeenCalledWith('/library/mcp/test', { server: 'gh' })
    expect(await screen.findByText(/z \(host offline\)/)).toBeTruthy()
  })

  it('Test all probes without a server filter', async () => {
    vi.mocked(api.post).mockResolvedValue({ rows: [], skipped: [] })
    mockServers([])
    renderSection()
    await userEvent.click(await screen.findByRole('button', { name: 'Test all' }))
    expect(api.post).toHaveBeenCalledWith('/library/mcp/test', {})
  })

  it('shows a readable message when the probe times out', async () => {
    const { ApiRequestError } = await import('@/api/client')
    vi.mocked(api.post).mockRejectedValue(new ApiRequestError(504, { error: 'mcp_probe_timeout' }))
    mockServers([srv('gh', 'stdio', [])])
    renderSection()
    await userEvent.click(await screen.findByRole('button', { name: 'Test gh' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/timed out/i)
  })

  it('offers Reauth only for http/sse rows that need auth', async () => {
    mockServers([srv('notion', 'http', [row({ status: 'needs-auth' })]), srv('ok', 'http', [row({ server: 'ok' })])])
    renderSection()
    expect(await screen.findAllByRole('button', { name: /fix auth/i })).toHaveLength(1)
  })

  it('lets the user pick which needs-auth account to reauthorize', async () => {
    mockServers([srv('notion', 'http', [row({ account: 'a', status: 'needs-auth' }), row({ account: 'b', status: 'needs-auth' })])])
    renderSection()
    await userEvent.selectOptions(await screen.findByRole('combobox'), 'b')
    await userEvent.click(screen.getByRole('button', { name: /fix auth/i }))
    expect(api.post).toHaveBeenCalledWith('/library/mcp/notion/auth', { account: 'b' })
  })

  it('renders an unknown status verbatim as neutral', async () => {
    mockServers([srv('x', 'http', [row({ status: 'weird-state' })])])
    renderSection()
    expect(await screen.findByText('weird-state')).toBeTruthy()
  })

  it('shows credential guidance instead of OAuth for stdio servers', async () => {
    renderSection()
    expect(await screen.findByText(/configure credentials on the host/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /fix auth|retry/i })).toBeNull()
    expect(api.post).not.toHaveBeenCalled()
  })

  it.each(['http', 'sse'])('offers OAuth for %s servers needing auth', async (transport) => {
    mockServers([{
      ...mcpResponse.servers[0], transport,
      config: { type: transport, url: 'https://example.com/mcp' },
    }])
    renderSection()
    await userEvent.click(await screen.findByRole('button', { name: /fix auth/i }))
    expect(api.post).toHaveBeenCalledWith('/library/mcp/my-server/auth', { account: 'acct1' })
  })

  it('keys each row\'s scope control by serverKey (manifest name for plugins), outside the card button', async () => {
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path === '/library/mcp') {
        return Promise.resolve({ servers: [{
          name: 'notion', source: 'notion@claude-plugins-official', pluginName: 'Notion', readonly: true,
          transport: 'http', config: { type: 'http', url: 'https://x' }, status: [],
        }] })
      }
      if (path === '/library/mcp/scope') {
        return Promise.resolve({
          servers: { 'plugin:Notion:notion': { defaultEnabled: false } },
          rules: [{ server: 'plugin:Notion:notion', repo: 'acme/web', enabled: true }],
        })
      }
      return otherGet(path)
    })
    renderSection()
    const off = await screen.findByRole('radio', { name: 'Off by default' })
    expect(off).toHaveProperty('checked', true)
    expect(screen.getByText('acme/web — on')).toBeTruthy()
    expect(off.closest('button')).toBeNull()
  })

  // (a) user + plugin rows render with transport and needs-auth badges
  it('renders rows with transport badges and the status pill', async () => {
    renderSection()
    // User server row
    expect(await screen.findByText('my-server')).toBeTruthy()
    // Transport badges (multiple stdio servers now, use findAllByText)
    expect((await screen.findAllByText('stdio')).length).toBeGreaterThan(0)
    expect((await screen.findAllByText('http')).length).toBeGreaterThan(0)
    // Plugin source badge
    expect(await screen.findByText('alpha@m')).toBeTruthy()
    // Worst-status pill for my-server
    expect(await screen.findByText('Needs auth')).toBeTruthy()
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
    renderSection()

    // Click the user server row to open edit dialog
    const serverRow = await screen.findByRole('button', { name: 'my-server' })
    await userEvent.click(serverRow)

    // Dialog opens
    expect(await screen.findByRole('dialog')).toBeTruthy()

    // Click Delete — it asks in a ConfirmDialog over the editor (2026-09-30
    // Settings critique, P1 — was window.confirm).
    const deleteBtn = await screen.findByRole('button', { name: /^delete$/i })
    await userEvent.click(deleteBtn)
    expect(api.del).not.toHaveBeenCalled()
    await userEvent.click(await screen.findByRole('button', { name: 'Delete my-server' }))

    await waitFor(() => {
      expect(api.del).toHaveBeenCalledWith('/library/mcp/my-server')
    })
  })

  // (d) Plugin server dialog has no Save button
  it('plugin server dialog has no Save button', async () => {
    renderSection()

    const pluginRow = await screen.findByRole('button', { name: 'plugin-server' })
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

    const serverRow = await screen.findByRole('button', { name: 'httpurl-server' })
    await userEvent.click(serverRow)

    expect(await screen.findByRole('dialog')).toBeTruthy()

    // The URL input should show the httpUrl value (seeded from httpUrl fallback)
    const urlInput = screen.getByRole('textbox', { name: /^url$/i })
    expect((urlInput as HTMLInputElement).value).toBe('https://via-httpurl.example/mcp')
  })

  // (f) Entering JSON mode with a pristine edit form shows full server.config
  it('entering JSON mode with pristine edit form shows full server.config', async () => {
    renderSection()

    const serverRow = await screen.findByRole('button', { name: 'my-server' })
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

    const serverRow = await screen.findByRole('button', { name: 'extra-keys-server' })
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
