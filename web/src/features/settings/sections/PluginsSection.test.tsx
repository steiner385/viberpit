import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const CONSENT =
  'Plugins run their own code — hooks and MCP servers — in every session on every host, including unattended automations. Install only plugins you trust.'
const RESTART = 'Running sessions pick this up when they restart.'

const state = vi.hoisted(() => ({ installEnabled: true }))

vi.mock('@/api/client', () => {
  class ApiRequestError extends Error {
    status: number
    body: Record<string, unknown>
    constructor(status: number, body: Record<string, unknown>) {
      super(`API error ${status}`)
      this.status = status
      this.body = body
    }
  }
  return {
    ApiRequestError,
    api: {
      get: vi.fn(),
      post: vi.fn(),
      put: vi.fn(),
      patch: vi.fn(),
      del: vi.fn(),
    },
  }
})

import PluginsSection from './PluginsSection'
import { api, ApiRequestError } from '@/api/client'
import { QK } from '@/api/hooks'
import { SETTINGS_BY_ID } from '../settingsDefs'

const ALPHA = {
  id: 'alpha@m',
  name: 'alpha',
  marketplace: 'm',
  enabled: true,
  version: '1.0.0',
  description: 'Alpha plugin',
  category: 'tools',
  components: { commands: 1, skills: 0, agents: 0, mcpServers: 0, hooks: 0 },
  tokenCost: { alwaysOn: 200, onInvoke: 100 },
  autoUpdate: true,
}

const MARKETS = [
  { name: 'm', source: { source: 'github', repo: 'acme/m' }, autoUpdate: false,
    normalizedSource: 'github:acme/m', confirmedSource: 'github:acme/m', curation: 'confirmed' },
  { name: 'handmade', source: { source: 'github', repo: 'evil/handmade' }, autoUpdate: false,
    normalizedSource: 'github:evil/handmade', confirmedSource: null, curation: 'unconfirmed' },
]

const BROWSE = [
  { pluginId: 'hooky@m', name: 'hooky', description: '<b>bold</b> claims', marketplace: 'm', version: '2.0.0',
    reviewed: false, components: { hooks: 2, mcpServers: 1, lspServers: 0, agents: 0, skills: 3 }, installed: false },
  { pluginId: 'dir@anthropic-plugin-directory', name: 'dir', description: 'Reviewed one',
    marketplace: 'anthropic-plugin-directory', version: null, reviewed: true, components: null, installed: false,
    source: 'https://github.com/acme/dir @ 8ca22dba9a94' },
  { pluginId: 'alpha@m', name: 'alpha', description: 'Alpha plugin', marketplace: 'm', version: '1.0.0',
    reviewed: false, components: null, installed: true },
]

function hosts(...rows: [string, 'ok' | 'noop' | 'failed', string?][]) {
  return rows.map(([host, outcome, failureCode]) => ({ host, outcome, ...(failureCode ? { failureCode, message: 'boom' } : {}) }))
}

beforeEach(() => {
  state.installEnabled = true
  vi.mocked(api.get).mockReset().mockImplementation((path: string) => {
    if (path === '/library/plugins') {
      return Promise.resolve({ plugins: [ALPHA], marketplaces: MARKETS, installEnabled: state.installEnabled })
    }
    if (path.startsWith('/library/plugins/browse')) {
      const q = new URLSearchParams(path.split('?')[1]).get('q') ?? ''
      const rows = BROWSE.filter((b) => b.name.includes(q))
      return Promise.resolve({ plugins: rows, total: rows.length, offset: 0, limit: 20, installEnabled: true })
    }
    if (path === '/library/plugins/alpha%40m') {
      return Promise.resolve({
        ...ALPHA,
        commands: [{ path: 'do/thing', description: 'Does a thing' }],
        skills: [], agents: [], mcpServers: [], hasHooks: false,
      })
    }
    return Promise.resolve({})
  })
  vi.mocked(api.patch).mockReset().mockResolvedValue({
    id: 'alpha@m', enabled: false, op: 'disable', ok: true, hosts: hosts(['dobby', 'ok'], ['rocky', 'noop']),
  })
  vi.mocked(api.post).mockReset()
  vi.mocked(api.del).mockReset()
})

function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    qc,
    ...render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <PluginsSection />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  }
}

/** A promise the test settles by hand, to hold a mutation in flight. */
function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => { resolve = r })
  return { promise, resolve }
}

const LOAD_FAILED = 'Couldn’t load installed plugins.'

describe('PluginsSection — installed plugins', () => {
  it('renders plugin rows with name', async () => {
    renderSection()
    expect(await screen.findByRole('button', { name: /^alpha/ })).toBeTruthy()
  })

  it('shows version, category, component summary and auto-updates (D3)', async () => {
    renderSection()
    const row = (await screen.findByRole('button', { name: /^alpha/ })).closest('li')!
    expect(within(row).getByText('1.0.0')).toBeTruthy()
    expect(within(row).getByText('tools')).toBeTruthy()
    expect(within(row).getByText('1 commands')).toBeTruthy()
    expect(within(row).getByText('auto-updates')).toBeTruthy()
  })

  it('toggle switch calls PATCH with encoded id and new state, then lists each host', async () => {
    renderSection()
    const sw = await screen.findByRole('switch', { name: 'Enable alpha' })
    await userEvent.click(sw)
    expect(api.patch).toHaveBeenCalledWith('/library/plugins/alpha%40m', { enabled: false })
    const results = await screen.findByRole('group', { name: 'Host results' })
    expect(within(results).getByText('dobby')).toBeTruthy()
    expect(within(results).getByText('no change')).toBeTruthy()
  })

  it('shows the new state and a saving status at once while the save is slow (#3067)', async () => {
    vi.mocked(api.patch).mockImplementationOnce(() => new Promise(() => {}))
    renderSection()
    const sw = await screen.findByRole('switch', { name: 'Enable alpha' })
    await userEvent.click(sw)
    expect(sw).not.toBeChecked()
    expect(sw).toBeDisabled()
    expect(screen.getByRole('status')).toHaveTextContent(/Saving/)
    expect(screen.queryByText('On')).toBeNull()
  })

  it('rolls back and says why when the save fails (#3067)', async () => {
    vi.mocked(api.patch).mockImplementationOnce(() => Promise.reject(new Error('disk full')))
    renderSection()
    const sw = await screen.findByRole('switch', { name: 'Enable alpha' })
    await userEvent.click(sw)
    expect(await screen.findByRole('alert')).toHaveTextContent(/Couldn.t save.*disk full/)
    await waitFor(() => expect(sw).toBeChecked())
    expect(sw).toBeEnabled()
  })

  it('expanding a row fetches plugin detail and shows a command', async () => {
    renderSection()
    await userEvent.click(await screen.findByRole('button', { name: /^alpha/ }))
    expect(await screen.findByText('/do:thing')).toBeTruthy()
  })

  it('shows token cost badge when tokenCost is present', async () => {
    renderSection()
    expect(await screen.findByText('≈200 tok always-on')).toBeTruthy()
  })

  it('Update opens the consent card and posts confirm: true', async () => {
    vi.mocked(api.post).mockResolvedValue({ op: 'update', ok: true, hosts: hosts(['dobby', 'ok']) })
    renderSection()
    await userEvent.click(await screen.findByRole('button', { name: 'Update alpha' }))
    const dialog = await screen.findByRole('dialog', { name: 'Update plugin' })
    expect(within(dialog).getByText(CONSENT)).toBeTruthy()
    expect(within(dialog).getByText('github:acme/m')).toBeTruthy()
    expect(api.post).not.toHaveBeenCalled()
    await userEvent.click(within(dialog).getByRole('button', { name: 'Update' }))
    expect(api.post).toHaveBeenCalledWith('/library/plugins/alpha%40m/update', { confirm: true })
    expect(await within(dialog).findByText(RESTART)).toBeTruthy()
  })

  it('the Update card never shows the installed version or its contents as the incoming ones', async () => {
    renderSection()
    await userEvent.click(await screen.findByRole('button', { name: 'Update alpha' }))
    const dialog = await screen.findByRole('dialog', { name: 'Update plugin' })
    expect(within(dialog).getByText('Contents unknown — inspect the marketplace before installing')).toBeTruthy()
    expect(within(dialog).queryByText(/1 commands/)).toBeNull()
    expect(within(dialog).queryByText(/1\.0\.0/)).toBeNull()
  })

  it('zero hosts answering is not done: says nothing changed and keeps Update offered', async () => {
    vi.mocked(api.post).mockResolvedValue({ op: 'update', ok: false, hosts: [] })
    renderSection()
    await userEvent.click(await screen.findByRole('button', { name: 'Update alpha' }))
    const dialog = await screen.findByRole('dialog', { name: 'Update plugin' })
    await userEvent.click(within(dialog).getByRole('button', { name: 'Update' }))
    expect(await within(dialog).findByText(/No host answered — nothing was changed/)).toBeTruthy()
    expect(within(dialog).queryByText(/done on/)).toBeNull()
    expect(within(dialog).queryByText(RESTART)).toBeNull()
    expect(within(dialog).getByRole('button', { name: 'Update' })).toBeTruthy()
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeTruthy()
  })

  it('Uninstall with zero hosts answering reports nothing changed', async () => {
    vi.mocked(api.del).mockResolvedValue({ op: 'uninstall', ok: false, hosts: [] })
    renderSection()
    await userEvent.click(await screen.findByRole('button', { name: 'Uninstall alpha' }))
    await userEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Uninstall' }))
    expect(await screen.findByText(/Uninstall alpha: No host answered — nothing was changed/)).toBeTruthy()
  })

  it('a failed GET /library/plugins says so with Retry, never "No plugins installed" (#3312)', async () => {
    let fail = true
    const ok = vi.mocked(api.get).getMockImplementation()!
    vi.mocked(api.get).mockImplementation((path: string) =>
      path === '/library/plugins' && fail ? Promise.reject(new ApiRequestError(503, {})) : ok(path))
    renderSection()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(LOAD_FAILED)
    expect(screen.queryByText('No plugins installed.')).toBeNull()
    fail = false
    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('button', { name: /^alpha/ })).toBeTruthy()
    expect(screen.queryByText(LOAD_FAILED)).toBeNull()
  })

  it('a failed refresh keeps the loaded rows and flags them stale (#3312)', async () => {
    const ok = vi.mocked(api.get).getMockImplementation()!
    const { qc } = renderSection()
    await screen.findByRole('button', { name: /^alpha/ })
    vi.mocked(api.get).mockImplementation((path: string) =>
      path === '/library/plugins' ? Promise.reject(new ApiRequestError(500, {})) : ok(path))
    await qc.refetchQueries({ queryKey: QK.libraryPlugins })
    expect(await screen.findByText(/Couldn’t load installed plugins\. Showing the last loaded data/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /^alpha/ })).toBeTruthy()
  })

  it('a plugin change invalidates detail and browse through the QK prefixes (#3312)', async () => {
    vi.mocked(api.del).mockResolvedValue({ op: 'uninstall', ok: true, hosts: hosts(['dobby', 'ok']) })
    const { qc } = renderSection()
    const spy = vi.spyOn(qc, 'invalidateQueries')
    await userEvent.click(await screen.findByRole('button', { name: 'Uninstall alpha' }))
    await userEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Uninstall' }))
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ queryKey: QK.libraryPluginAll }))
    expect(spy).toHaveBeenCalledWith({ queryKey: QK.libraryPluginBrowseAll })
  })

  it('the plugin detail and browse keys extend their invalidation prefixes (#3312)', () => {
    expect(QK.libraryPlugin('a@m').slice(0, QK.libraryPluginAll.length)).toEqual([...QK.libraryPluginAll])
    const browseKey = QK.libraryPluginBrowse({ q: '', marketplace: '', offset: 0, limit: 20 })
    expect(browseKey.slice(0, QK.libraryPluginBrowseAll.length)).toEqual([...QK.libraryPluginBrowseAll])
    expect(QK.libraryPlugins.slice(0, QK.libraryPluginAll.length)).not.toEqual([...QK.libraryPluginAll])
  })

  it('Uninstall asks first, then DELETEs and lists each host', async () => {
    vi.mocked(api.del).mockResolvedValue({ op: 'uninstall', ok: true, hosts: hosts(['dobby', 'ok'], ['rocky', 'ok']) })
    renderSection()
    await userEvent.click(await screen.findByRole('button', { name: 'Uninstall alpha' }))
    expect(api.del).not.toHaveBeenCalled()
    await userEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Uninstall' }))
    expect(api.del).toHaveBeenCalledWith('/library/plugins/alpha%40m')
    expect(await screen.findByText(/Uninstall alpha: done on all 2 hosts/)).toBeTruthy()
  })
})

describe('PluginsSection — marketplaces', () => {
  it('add shows the exact source before an explicit confirm, then posts {source, confirm: true}', async () => {
    vi.mocked(api.post).mockResolvedValue({ op: 'marketplace_add', ok: true, marketplaces: ['tools'],
      hosts: hosts(['dobby', 'ok']) })
    renderSection()
    const input = await screen.findByRole('textbox', { name: 'Marketplace source' })
    await userEvent.type(input, '  acme/tools  ')
    await userEvent.click(screen.getByRole('button', { name: 'Review source' }))
    const review = screen.getByRole('group', { name: 'Review marketplace source' })
    expect(within(review).getByText('acme/tools')).toBeTruthy()
    expect(within(review).getByText(/Every host fetches this source/)).toBeTruthy()
    expect(api.post).not.toHaveBeenCalled()
    await userEvent.click(within(review).getByRole('button', { name: 'Confirm and add' }))
    expect(api.post).toHaveBeenCalledWith('/library/marketplaces', { source: 'acme/tools', confirm: true })
    expect(await screen.findByText(/Add acme\/tools: done on the host/)).toBeTruthy()
  })

  it('Cancel on the add review is disabled while the add is in flight (#3312)', async () => {
    const pending = deferred<unknown>()
    vi.mocked(api.post).mockReturnValue(pending.promise as never)
    renderSection()
    await userEvent.type(await screen.findByRole('textbox', { name: 'Marketplace source' }), 'acme/tools')
    await userEvent.click(screen.getByRole('button', { name: 'Review source' }))
    const review = screen.getByRole('group', { name: 'Review marketplace source' })
    await userEvent.click(within(review).getByRole('button', { name: 'Confirm and add' }))
    expect(within(review).getByRole('button', { name: 'Cancel' })).toBeDisabled()
    pending.resolve({ op: 'marketplace_add', ok: true, marketplaces: ['tools'], hosts: hosts(['dobby', 'ok']) })
    expect(await screen.findByText(/Add acme\/tools: done on the host/)).toBeTruthy()
  })

  it('editing the source after Review drops the review', async () => {
    renderSection()
    const input = await screen.findByRole('textbox', { name: 'Marketplace source' })
    await userEvent.type(input, 'acme/tools')
    await userEvent.click(screen.getByRole('button', { name: 'Review source' }))
    await userEvent.type(input, 'x')
    expect(screen.queryByRole('group', { name: 'Review marketplace source' })).toBeNull()
  })

  it('an unconfirmed marketplace shows its source verbatim; Confirm echoes it', async () => {
    vi.mocked(api.post).mockResolvedValue({ name: 'handmade', confirmedSource: 'github:evil/handmade', curation: 'confirmed' })
    renderSection()
    const row = (await screen.findByText('handmade')).closest('li')!
    expect(within(row).getByText('Unconfirmed')).toBeTruthy()
    expect(within(row).getByText('github:evil/handmade')).toBeTruthy()
    await userEvent.click(within(row).getByRole('button', { name: 'Confirm' }))
    const dialog = await screen.findByRole('dialog', { name: 'Confirm marketplace' })
    expect(within(dialog).getByText('github:evil/handmade')).toBeTruthy()
    await userEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }))
    expect(api.post).toHaveBeenCalledWith('/library/marketplaces/handmade/confirm',
      { source: 'github:evil/handmade', confirm: true })
  })

  it('a stale Confirm (409 source_changed) says so and refetches', async () => {
    vi.mocked(api.post).mockRejectedValue(new ApiRequestError(409, { detail: {
      code: 'PLUGIN_MARKETPLACE_NOT_CURATED', message: 'x', reason: 'source_changed' } }))
    renderSection()
    const row = (await screen.findByText('handmade')).closest('li')!
    const before = vi.mocked(api.get).mock.calls.filter(([p]) => p === '/library/plugins').length
    await userEvent.click(within(row).getByRole('button', { name: 'Confirm' }))
    await userEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Confirm' }))
    expect(await within(row).findByRole('alert'))
      .toHaveTextContent('The source changed since this page loaded — review it again')
    await waitFor(() => expect(
      vi.mocked(api.get).mock.calls.filter(([p]) => p === '/library/plugins').length).toBeGreaterThan(before))
  })

  it('Update and Remove post to the marketplace routes', async () => {
    vi.mocked(api.post).mockResolvedValue({ op: 'marketplace_update', ok: true, hosts: hosts(['dobby', 'ok']) })
    vi.mocked(api.del).mockResolvedValue({ op: 'marketplace_remove', ok: true, hosts: hosts(['dobby', 'ok']) })
    renderSection()
    const row = (await screen.findByText('github:acme/m')).closest('li')!
    await userEvent.click(within(row).getByRole('button', { name: 'Update' }))
    expect(api.post).toHaveBeenCalledWith('/library/marketplaces/m/update', {})
    await userEvent.click(within(row).getByRole('button', { name: 'Remove m' }))
    await userEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Remove' }))
    expect(api.del).toHaveBeenCalledWith('/library/marketplaces/m')
  })
})

describe('PluginsSection — browse and install', () => {
  it('renders entries with the Reviewed badge, components and Installed state; untrusted text stays text', async () => {
    const { container } = renderSection()
    const hooky = (await screen.findByText('hooky')).closest('li')!
    expect(within(hooky).getByText('2 hooks · 1 MCP servers · 3 skills')).toBeTruthy()
    expect(within(hooky).getByText('<b>bold</b> claims')).toBeTruthy()
    expect(container.querySelector('b')).toBeNull()
    const dir = screen.getByText('dir').closest('li')!
    expect(within(dir).getByText('Reviewed')).toBeTruthy()
    expect(within(dir).getByText('Contents unknown — inspect the marketplace before installing')).toBeTruthy()
    expect(within(hooky).queryByText('Reviewed')).toBeNull()
    expect(screen.getByText('Installed')).toBeTruthy()
  })

  it('search hits the server with a debounced ?q=', async () => {
    renderSection()
    await screen.findByText('hooky')
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search plugins' }), 'dir')
    await waitFor(() => expect(vi.mocked(api.get).mock.calls.some(([p]) =>
      typeof p === 'string' && p.startsWith('/library/plugins/browse?') && p.includes('q=dir'))).toBe(true))
    await waitFor(() => expect(screen.queryByText('hooky')).toBeNull())
  })

  it('the marketplace filter is sent to the server', async () => {
    renderSection()
    await screen.findByText('hooky')
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Marketplace' }), 'anthropic-plugin-directory')
    await waitFor(() => expect(vi.mocked(api.get).mock.calls.some(([p]) =>
      typeof p === 'string' && p.includes('marketplace=anthropic-plugin-directory'))).toBe(true))
  })

  it('Install opens the consent card with the exact copy, posts confirm: true, and shows a failed host with Retry', async () => {
    vi.mocked(api.post)
      .mockResolvedValueOnce({ op: 'install', ok: false, hosts: hosts(['dobby', 'ok'], ['rocky', 'failed', 'host_unreachable']) })
      .mockResolvedValueOnce({ op: 'install', ok: true, hosts: hosts(['dobby', 'noop'], ['rocky', 'ok']) })
    renderSection()
    await userEvent.click(await screen.findByRole('button', { name: 'Install hooky' }))
    const dialog = await screen.findByRole('dialog', { name: 'Install plugin' })
    expect(within(dialog).getByText(CONSENT)).toBeTruthy()
    expect(within(dialog).getByText('2 hooks · 1 MCP servers · 3 skills')).toBeTruthy()
    expect(within(dialog).getByText('github:acme/m')).toBeTruthy()
    expect(api.post).not.toHaveBeenCalled()
    await userEvent.click(within(dialog).getByRole('button', { name: 'Install' }))
    expect(api.post).toHaveBeenCalledWith('/library/plugins/hooky%40m/install', { confirm: true })
    const results = await within(dialog).findByRole('group', { name: 'Host results' })
    expect(within(results).getByText(/failed on 1 of 2 hosts/)).toBeTruthy()
    expect(within(results).getByText('host_unreachable')).toBeTruthy()
    expect(within(results).getByText('failed')).toBeTruthy()
    expect(within(dialog).getByText(RESTART)).toBeTruthy()
    await userEvent.click(within(results).getByRole('button', { name: 'Retry' }))
    expect(api.post).toHaveBeenCalledTimes(2)
    expect(vi.mocked(api.post).mock.calls[1]).toEqual(['/library/plugins/hooky%40m/install', { confirm: true }])
    expect(await within(dialog).findByText(/done on all 2 hosts/)).toBeTruthy()
  })

  it('the marketplace filter falls back to all when its marketplace stops being confirmed (#3312)', async () => {
    const ok = vi.mocked(api.get).getMockImplementation()!
    const { qc } = renderSection()
    await screen.findByText('hooky')
    const filter = screen.getByRole('combobox', { name: 'Marketplace' })
    await userEvent.selectOptions(filter, 'm')
    await waitFor(() => expect(vi.mocked(api.get).mock.calls.some(([p]) =>
      typeof p === 'string' && p.includes('marketplace=m'))).toBe(true))
    // The marketplace's source changed: it is no longer a confirmed option.
    vi.mocked(api.get).mockImplementation((path: string) => path === '/library/plugins'
      ? Promise.resolve({ plugins: [ALPHA], installEnabled: true,
        marketplaces: [{ ...MARKETS[0], normalizedSource: 'github:evil/m', curation: 'source_changed' }, MARKETS[1]] })
      : ok(path))
    await qc.refetchQueries({ queryKey: QK.libraryPlugins })
    await waitFor(() => expect(filter).toHaveValue(''))
    expect(within(filter).queryByRole('option', { name: 'm' })).toBeNull()
    // The page now reads the unfiltered first page, not the vanished marketplace's.
    const watched = qc.getQueryCache().findAll({ queryKey: QK.libraryPluginBrowseAll })
      .filter((query) => query.getObserversCount() > 0)
    expect(watched.map((query) => query.queryKey))
      .toEqual([QK.libraryPluginBrowse({ q: '', marketplace: '', offset: 0, limit: 20 })])
  })

  it('the consent card cannot be cancelled or dismissed while the install is in flight (#3312)', async () => {
    const pending = deferred<unknown>()
    vi.mocked(api.post).mockReturnValue(pending.promise as never)
    renderSection()
    await userEvent.click(await screen.findByRole('button', { name: 'Install hooky' }))
    const dialog = await screen.findByRole('dialog', { name: 'Install plugin' })
    await userEvent.click(within(dialog).getByRole('button', { name: 'Install' }))
    const cancel = within(dialog).getByRole('button', { name: 'Cancel' })
    expect(cancel).toBeDisabled()
    await userEvent.keyboard('{Escape}')
    await userEvent.click(screen.getByTestId('dialog-backdrop'))
    expect(screen.getByRole('dialog', { name: 'Install plugin' })).toBeTruthy()
    pending.resolve({ op: 'install', ok: true, hosts: hosts(['dobby', 'ok']) })
    expect(await within(dialog).findByText(/done on the host/)).toBeTruthy()
    await userEvent.click(within(dialog).getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog', { name: 'Install plugin' })).toBeNull()
  })

  it('an install with unknown contents says so on the consent card', async () => {
    renderSection()
    await userEvent.click(await screen.findByRole('button', { name: 'Install dir' }))
    const dialog = await screen.findByRole('dialog', { name: 'Install plugin' })
    expect(within(dialog).getByText('Contents unknown — inspect the marketplace before installing')).toBeTruthy()
  })

  it('a Directory install shows the reviewed repository the server sent (#3312)', async () => {
    renderSection()
    await userEvent.click(await screen.findByRole('button', { name: 'Install dir' }))
    const dialog = await screen.findByRole('dialog', { name: 'Install plugin' })
    expect(within(dialog).getByText('https://github.com/acme/dir @ 8ca22dba9a94')).toBeTruthy()
    expect(within(dialog).queryByText(/source not shown by this host/)).toBeNull()
  })
})

describe('PluginsSection — kill switch and engines', () => {
  it('installEnabled=false hides install, update, add and confirm; enable, uninstall and remove stay', async () => {
    state.installEnabled = false
    renderSection()
    expect(await screen.findByText(/Installing, updating and adding marketplaces is turned off/)).toBeTruthy()
    expect(screen.queryByRole('textbox', { name: 'Marketplace source' })).toBeNull()
    expect(screen.queryByRole('searchbox', { name: 'Search plugins' })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Install/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Update alpha' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Confirm' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Update' })).toBeNull()
    expect(screen.getByRole('switch', { name: 'Enable alpha' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Uninstall alpha' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Remove m' })).toBeTruthy()
    expect(vi.mocked(api.get).mock.calls.some(([p]) => typeof p === 'string' && p.startsWith('/library/plugins/browse')))
      .toBe(false)
  })

  it('is Claude only: lives in the Claude config group and never offers Codex', async () => {
    expect(SETTINGS_BY_ID.plugins.group).toBe('claude')
    renderSection()
    await screen.findByText('hooky')
    expect(screen.queryByText(/codex/i)).toBeNull()
  })
})
