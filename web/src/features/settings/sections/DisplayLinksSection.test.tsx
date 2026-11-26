import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { AssistantTokenRow } from '@/api/displayTokens'

let me: unknown = { identity: 'alice@example.com', scope: 'full', authMode: 'tailscale' }
let rows: AssistantTokenRow[] = []

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      get: vi.fn(async (path: string): Promise<unknown> => {
        if (path === '/auth/me') return me
        if (path === '/assistant/tokens') return rows
        throw new Error(`unexpected GET ${path}`)
      }),
      post: vi.fn(async (_path: string, body: { label: string }) => ({
        ...row('new', 'display', { label: body.label }), token: 'ccas2:fresh-secret',
      })),
      put: vi.fn(),
      patch: vi.fn(),
      del: vi.fn(async () => ({ ok: true })),
    },
  }
})

import { api } from '@/api/client'
import { DisplayLinksSection } from './DisplayLinksSection'

function row(id: string, scope: AssistantTokenRow['scope'], over: Partial<AssistantTokenRow> = {}): AssistantTokenRow {
  return {
    id, scope, label: `label-${id}`,
    createdAt: '2026-11-20T10:00:00Z', expiresAt: '2026-12-20T10:00:00Z',
    revokedAt: null, lastUsedAt: null, ...over,
  }
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><DisplayLinksSection /></QueryClientProvider>)
}

beforeEach(() => {
  vi.clearAllMocks()
  me = { identity: 'alice@example.com', scope: 'full', authMode: 'tailscale' }
  rows = [
    row('wall', 'display', { label: 'Kitchen TV', lastUsedAt: '2026-11-25T09:30:00Z' }),
    row('cli', 'read', { label: 'laptop mcp' }),
    row('gone', 'display', { label: 'Old lobby', revokedAt: '2026-11-21T00:00:00Z' }),
  ]
})

describe('DisplayLinksSection (#1408 AC-G10.9)', () => {
  it('lists unrevoked display tokens with label, created, expiry and last used', async () => {
    mount()
    const label = await screen.findByText('Kitchen TV')
    const tr = label.closest('tr')!
    expect(within(tr).getAllByRole('time')).toHaveLength(3)
    expect(tr.querySelector('time[datetime="2026-12-20T10:00:00Z"]')).not.toBeNull()
    expect(tr.querySelector('time[datetime="2026-11-25T09:30:00Z"]')).not.toBeNull()
    // Other scopes and revoked rows are not display links.
    expect(screen.queryByText('laptop mcp')).toBeNull()
    expect(screen.queryByText('Old lobby')).toBeNull()
  })

  it('revoke calls the existing revoke route', async () => {
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke Kitchen TV' }))
    await vi.waitFor(() => expect(api.del).toHaveBeenCalledWith('/assistant/tokens/wall'))
  })

  it('mints a display token and shows its /kiosk link once', async () => {
    mount()
    await screen.findByText('Kitchen TV')
    fireEvent.change(screen.getByLabelText(/Label/), { target: { value: 'Hall' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create link' }))
    const link = await screen.findByLabelText('Display link')
    expect(api.post).toHaveBeenCalledWith('/assistant/tokens', { scope: 'display', label: 'Hall', ttlDays: 30 })
    expect((link as HTMLInputElement).value).toBe(
      `${window.location.origin}/kiosk?${new URLSearchParams({ token: 'ccas2:fresh-secret' })}`)
  })

  it('is absent for a display-scoped session', async () => {
    me = { identity: 'display:Wall', scope: 'display', authMode: 'display' }
    const { container } = mount()
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledWith('/auth/me'))
    await vi.waitFor(() => expect(container).toBeEmptyDOMElement())
    expect(api.get).not.toHaveBeenCalledWith('/assistant/tokens')
  })
})
