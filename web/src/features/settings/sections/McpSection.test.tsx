import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
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

const toastMock = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))
vi.mock('sonner', () => ({ toast: Object.assign(vi.fn(), toastMock) }))

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
      overall: { status: 'needs-auth', error: null, grant: 'never' },
      status: ['acct1', 'acct2'].map((account) => ({
        account, host: 'dobby', server: 'my-server', status: 'needs-auth', error: null, toolCount: null,
        scope: 'user', source: 'probe' as const, sessionId: null, observedAt: '2026-10-09T12:00:00Z',
        counted: true,
      })),
    },
    {
      name: 'plugin-server',
      source: 'alpha@m',
      readonly: true,
      transport: 'http' as const,
      config: { type: 'http', url: 'https://plugin.example/mcp' },
      overall: { status: null, error: null, grant: 'none' },
      status: [],
    },
    {
      name: 'httpurl-server',
      source: 'user',
      readonly: false,
      transport: 'http' as const,
      config: { type: 'http', httpUrl: 'https://via-httpurl.example/mcp' },
      overall: { status: null, error: null, grant: 'none' },
      status: [],
    },
    {
      name: 'extra-keys-server',
      source: 'user',
      readonly: false,
      transport: 'stdio' as const,
      config: { type: 'stdio', command: 'my-cmd', args: [], customKey: 'preserve-me' },
      overall: { status: null, error: null, grant: 'none' },
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
    scope: 'user', source: 'probe' as const, sessionId: null, observedAt: '2026-10-09T12:00:00Z',
    counted: true, ...over,
  }
}

function mockServers(servers: unknown[]) {
  vi.mocked(api.get).mockImplementation((path: string) =>
    path === '/library/mcp' ? Promise.resolve({ servers }) : otherGet(path))
}

const NO_OVERALL = { status: null, error: null, grant: 'none' }

const srv = (name: string, transport: string, status: unknown[], overall: unknown = NO_OVERALL) => ({
  name, source: 'user', readonly: false, transport, config: { type: transport }, overall, status,
})

describe('McpSection', () => {
  it('shows the overall status pill and per-account detail', async () => {
    mockServers([srv('notion', 'http', [row({ account: 'a' }), row({ account: 'b', status: 'needs-auth', error: 'token expired' })],
      { status: 'needs-auth', error: null, grant: 'expired' })])
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
    mockServers([
      srv('notion', 'http', [row({ status: 'needs-auth' })], { status: 'needs-auth', error: null, grant: 'never' }),
      srv('ok', 'http', [row({ server: 'ok' })], { status: 'connected', error: null, grant: 'live' }),
    ])
    renderSection()
    expect(await screen.findAllByRole('button', { name: /fix auth/i })).toHaveLength(1)
  })

  it('shows the overall status, not the worst row', async () => {
    mockServers([srv('notion', 'http', [row({ status: 'needs-auth', counted: false })],
      { status: 'connected', error: null, grant: 'live' })])
    renderSection()
    expect(await screen.findByText('Connected')).toBeTruthy()
    expect(screen.queryByText('Needs auth')).toBeNull()
  })

  it('offers one Fix auth with no account picker when grant is never', async () => {
    mockServers([srv('notion', 'http', [row({ account: 'a', status: 'needs-auth' }), row({ account: 'b', status: 'needs-auth' })],
      { status: 'needs-auth', error: null, grant: 'never' })])
    renderSection()
    await userEvent.click(await screen.findByRole('button', { name: /fix auth/i }))
    expect(screen.queryByLabelText(/^account to reauth/i)).toBeNull()
    expect(screen.queryByRole('combobox')).toBeNull()
    expect(api.post).toHaveBeenCalledWith('/library/mcp/notion/auth', undefined)
  })

  it('renders no Fix auth when the server failed for a non-auth reason', async () => {
    mockServers([srv('notion', 'http', [row({ status: 'failed' })],
      { status: 'failed', error: 'Incompatible auth server…', grant: 'never' })])
    renderSection()
    expect(await screen.findByText('Incompatible auth server…')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /fix auth/i })).toBeNull()
  })

  it('offers Fix auth when needs-auth with a dead grant', async () => {
    mockServers([srv('notion', 'http', [row({ status: 'needs-auth' })],
      { status: 'needs-auth', error: null, grant: 'dead' })])
    renderSection()
    expect(await screen.findByRole('button', { name: /fix auth/i })).toBeTruthy()
  })

  it('renders no Fix auth for grant none', async () => {
    mockServers([srv('notion', 'http', [row({ status: 'needs-auth' })],
      { status: 'needs-auth', error: null, grant: 'none' })])
    renderSection()
    expect(await screen.findByText('Needs auth')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /fix auth/i })).toBeNull()
  })

  it('shows overall.error under the card', async () => {
    mockServers([srv('notion', 'http', [], { status: 'failed', error: 'refresh token revoked', grant: 'dead' })])
    renderSection()
    expect(await screen.findByText('refresh token revoked')).toBeTruthy()
  })

  it('marks uncounted rows in details', async () => {
    mockServers([srv('notion', 'http', [row({ account: 'a' }), row({ account: 'b', counted: false })],
      { status: 'connected', error: null, grant: 'live' })])
    renderSection()
    await userEvent.click(await screen.findByRole('button', { name: /details for notion/i }))
    expect(screen.getAllByText(/not counted/)).toHaveLength(1)
  })

  it('renders an unknown status verbatim as neutral', async () => {
    mockServers([srv('x', 'http', [row({ status: 'weird-state' })], { status: 'weird-state', error: null, grant: 'none' })])
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
    expect(api.post).toHaveBeenCalledWith('/library/mcp/my-server/auth', undefined)
  })

  it('keys each row\'s scope control by serverKey (manifest name for plugins), outside the card button', async () => {
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path === '/library/mcp') {
        return Promise.resolve({ servers: [{
          name: 'notion', source: 'notion@claude-plugins-official', pluginName: 'Notion', readonly: true,
          transport: 'http', config: { type: 'http', url: 'https://x' },
          overall: { status: null, error: null, grant: 'none' }, status: [],
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

  it('a failing scope GET leaves the row usable and shows a retryable alert', async () => {
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path === '/library/mcp/scope') return Promise.reject(new ApiRequestError(500, { detail: 'boom' }))
      return path === '/library/mcp'
        ? Promise.resolve({ servers: [srv('gh', 'stdio', [])] })
        : otherGet(path)
    })
    renderSection()
    expect((await screen.findByRole('alert')).textContent).toContain("Couldn't load repo scope.")
    expect(screen.getByRole('button', { name: 'gh' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Test gh' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
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
    // Overall-status pill for my-server
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

  describe('one-tap sign-in', () => {
    const needsAuth = () => srv('notion', 'http', [row({ status: 'needs-auth' })],
      { status: 'needs-auth', error: null, grant: 'expired' })

    function mockConfig(oneTapEnabled: boolean) {
      vi.mocked(api.get).mockImplementation((path: string) => {
        if (path === '/library/mcp') return Promise.resolve({ servers: [needsAuth()] })
        if (path === '/config') return Promise.resolve({ oneTapEnabled })
        return otherGet(path)
      })
    }

    afterEach(() => window.history.replaceState(null, '', '/'))

    it('posts Fix auth to the one-tap start route when enabled', async () => {
      mockConfig(true)
      renderSection()
      const button = await screen.findByRole('button', { name: 'Sign in' })
      const form = button.closest('form')!
      expect(form.getAttribute('method')).toBe('post')
      expect(form.getAttribute('action')).toBe('/api/mcp/oauth/start?server=notion')
    })

    it('keeps the McpAuthFlow when one-tap is off', async () => {
      mockConfig(false)
      renderSection()
      await screen.findByText('Needs auth')
      expect(screen.queryByRole('button', { name: 'Sign in' })).toBeNull()
      expect(await screen.findByRole('button', { name: 'Fix auth' })).toBeTruthy()
    })

    it('opens the paste-back flow for ?pasteBack even with one-tap on, and cleans the URL', async () => {
      mockConfig(true)
      window.history.replaceState(null, '', '/settings/mcp?pasteBack=notion')
      renderSection()
      await screen.findByText('Needs auth')
      expect(await screen.findByRole('button', { name: 'Fix auth' })).toBeTruthy()
      expect(window.location.pathname).toBe('/settings/mcp')
      expect(window.location.search).toBe('')
    })

    it('explains the manual flow above it for ?pasteBack, and only then', async () => {
      const note = "One-tap sign-in isn't available for this server; sign in manually below."
      mockConfig(true)
      window.history.replaceState(null, '', '/settings/mcp?pasteBack=notion')
      const { unmount } = renderSection()
      expect(await screen.findByText(note)).toBeTruthy()
      unmount()
      mockConfig(false)
      renderSection()
      expect(await screen.findByRole('button', { name: 'Fix auth' })).toBeTruthy()
      expect(screen.queryByText(note)).toBeNull()
    })

    it('toasts success for ?signedIn and cleans the URL', async () => {
      mockConfig(true)
      window.history.replaceState(null, '', '/settings/mcp?signedIn=notion')
      renderSection()
      await waitFor(() => expect(toastMock.success).toHaveBeenCalledWith(
        'Signed in to notion — every account and runner can use it now.'))
      expect(window.location.search).toBe('')
    })

    it.each([
      ['state_invalid', 'That sign-in link expired or was already used. Try again.'],
      ['denied', 'Sign-in was cancelled at the provider.'],
      ['exchange_failed', "The provider didn't accept the sign-in. Try again."],
      ['registration_failed', "This provider doesn't allow ViberPit to register. Use the manual sign-in."],
      ['not_configured', "That server can't be signed in here."],
      ['<b>x</b>', 'Sign-in failed.'],
    ])('maps signInError=%s to a fixed message', async (code, message) => {
      mockConfig(true)
      window.history.replaceState(null, '', `/settings/mcp?signInError=${encodeURIComponent(code)}`)
      renderSection()
      await waitFor(() => expect(toastMock.error).toHaveBeenCalledWith(message))
      expect(window.location.search).toBe('')
    })
  })

  describe('tool defaults (D-16b)', () => {
    const ann = (over: Record<string, boolean | null> = {}) => ({ readOnly: null, destructive: null, openWorld: null, ...over })
    const seenAt = new Date(Date.now() - 3 * 60_000).toISOString()

    function withScope(servers: Record<string, unknown>) {
      vi.mocked(api.get).mockImplementation((path: string) => {
        if (path === '/library/mcp') return Promise.resolve({ servers: [srv('gh', 'stdio', []), srv('fresh', 'stdio', [])] })
        if (path === '/library/mcp/scope') return Promise.resolve({ servers, rules: [] })
        return otherGet(path)
      })
    }

    it('a seen list expands to switches with badges and "Seen …"; toggling PUTs the tool default', async () => {
      withScope({
        gh: { defaultEnabled: true, toolsSeenAt: seenAt, tools: [
          { name: 'list_issues', description: 'Lists issues', annotations: ann({ readOnly: true }), enabled: true, source: 'default' },
          { name: 'delete_repo', description: null, annotations: ann({ destructive: true }), enabled: true, source: 'default' },
        ] },
        fresh: { defaultEnabled: true, toolsSeenAt: null, tools: null },
      })
      // The server's state after the PUT, as the re-read returns it.
      vi.mocked(api.put).mockImplementation(async (_path: string, body: unknown) => {
        const scope = (await vi.mocked(api.get)('/library/mcp/scope')) as { servers: { gh: { tools: { name: string; enabled: boolean }[] } } }
        scope.servers.gh.tools.find((t) => t.name === 'delete_repo')!.enabled = (body as { enabled: boolean }).enabled
        return undefined
      })
      renderSection()
      const btn = await screen.findByRole('button', { name: 'Tools for gh' })
      expect(btn).toHaveTextContent('Tools (2)')
      await userEvent.click(btn)
      const panel = screen.getByRole('group', { name: 'Tool defaults for gh' })
      expect(panel.id).toBe(btn.getAttribute('aria-controls'))
      expect(panel).toHaveTextContent('Defaults for new and restarted Claude sessions · Seen 3m ago')
      expect(panel).toHaveTextContent('read-only')
      expect(panel).toHaveTextContent('destructive')
      expect(panel).not.toHaveTextContent('Session')
      await userEvent.click(screen.getByRole('switch', { name: 'delete_repo enabled' }))
      expect(api.put).toHaveBeenCalledWith('/library/mcp/gh/tools/delete_repo', { enabled: false })
      await waitFor(() =>
        expect(screen.getByRole('switch', { name: 'delete_repo enabled' })).toHaveAttribute('aria-checked', 'false'))
    })

    it('no seen list: the hint sits beside Test, with no switches', async () => {
      withScope({ gh: { defaultEnabled: true, tools: null, toolsSeenAt: null }, fresh: { defaultEnabled: true } })
      renderSection()
      const hints = await screen.findAllByText('Test the server to load its tools.')
      expect(hints).toHaveLength(2)
      expect(hints[0].parentElement).toContainElement(screen.getByRole('button', { name: 'Test gh' }))
      expect(screen.queryByRole('button', { name: /^Tools for/ })).toBeNull()
    })

    it('a refused PUT keeps the switch and shows the server text', async () => {
      withScope({
        gh: { defaultEnabled: true, toolsSeenAt: seenAt, tools: [
          { name: 'list_issues', description: null, annotations: ann(), enabled: true, source: 'default' },
        ] },
      })
      vi.mocked(api.put).mockRejectedValue(new ApiRequestError(403, {
        detail: { code: 'SESSION_CONTROL_HUMAN_ONLY', message: 'Only a person can change this.' },
      }))
      renderSection()
      await userEvent.click(await screen.findByRole('button', { name: 'Tools for gh' }))
      await userEvent.click(screen.getByRole('switch', { name: 'list_issues enabled' }))
      expect(await screen.findByText('Only a person can change this.')).toHaveAttribute('role', 'alert')
      expect(screen.getByRole('switch', { name: 'list_issues enabled' })).toHaveAttribute('aria-checked', 'true')
    })

    it('renders nothing while the scope feature is off', async () => {
      mockServers([srv('gh', 'stdio', [])])
      renderSection()
      await screen.findByRole('button', { name: 'Test gh' })
      expect(screen.queryByText('Test the server to load its tools.')).toBeNull()
      expect(screen.queryByRole('button', { name: /^Tools for/ })).toBeNull()
    })
  })
})
