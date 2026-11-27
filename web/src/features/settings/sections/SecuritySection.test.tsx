import { afterEach, describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PasskeyCredential } from '@/api/webauthn'

let me: unknown = { identity: 'alice@example.com', scope: 'full', authMode: 'tailscale' }
let config: unknown = { repoRoot: '/r', adoptionEnabled: false, webauthnEnabled: true }
let rows: PasskeyCredential[] = []

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      get: vi.fn(async (path: string): Promise<unknown> => {
        if (path === '/auth/me') return me
        if (path === '/config') return config
        if (path === '/auth/credentials') return rows
        throw new Error(`unexpected GET ${path}`)
      }),
      post: vi.fn(async (path: string, body: { label?: string }): Promise<unknown> => {
        if (path === '/auth/webauthn/register/begin') {
          return {
            ceremonyId: 'reg-1',
            publicKey: {
              challenge: 'AQID', rp: { id: 'localhost', name: 'ViberPit' },
              user: { id: 'Bw', name: 'alice@example.com', displayName: 'alice' },
              pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
            },
          }
        }
        if (path === '/auth/webauthn/register/complete') return cred('new', { label: body.label ?? null })
        throw new Error(`unexpected POST ${path}`)
      }),
      put: vi.fn(),
      patch: vi.fn(),
      del: vi.fn(async () => undefined),
    },
  }
})

const create = vi.fn()
const buf = (...xs: number[]) => new Uint8Array(xs).buffer
const fakeCredential = {
  id: 'AQ', rawId: buf(1), type: 'public-key', authenticatorAttachment: 'platform',
  response: { clientDataJSON: buf(2), attestationObject: buf(3), getTransports: () => ['internal'] },
  getClientExtensionResults: () => ({}),
}

import { api } from '@/api/client'
import { SecuritySection } from './SecuritySection'

function cred(id: string, over: Partial<PasskeyCredential> = {}): PasskeyCredential {
  return {
    id, label: `label-${id}`, transports: ['internal'],
    createdAt: '2026-11-20T10:00:00Z', lastUsedAt: null, ...over,
  }
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><SecuritySection /></QueryClientProvider>)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('PublicKeyCredential', class {})
  Object.defineProperty(navigator, 'credentials', { configurable: true, value: { get: vi.fn(), create } })
  create.mockResolvedValue(fakeCredential)
  me = { identity: 'alice@example.com', scope: 'full', authMode: 'tailscale' }
  config = { repoRoot: '/r', adoptionEnabled: false, webauthnEnabled: true }
  rows = [cred('mac', { label: 'MacBook', lastUsedAt: '2026-11-25T09:30:00Z' }), cred('key', { label: null })]
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('SecuritySection (#1410 AC-G12.11)', () => {
  it('lists passkeys with label, created and last used', async () => {
    mount()
    const tr = (await screen.findByText('MacBook')).closest('tr')!
    expect(within(tr).getAllByRole('time')).toHaveLength(2)
    expect(tr.querySelector('time[datetime="2026-11-25T09:30:00Z"]')).not.toBeNull()
    const unlabelled = screen.getByText('Unnamed passkey').closest('tr')!
    expect(within(unlabelled).getByText('Never')).toBeInTheDocument()
  })

  it('revokes only after the confirm', async () => {
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke MacBook' }))
    expect(api.del).not.toHaveBeenCalled()
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('MacBook')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Revoke passkey' }))
    await vi.waitFor(() => expect(api.del).toHaveBeenCalledWith('/auth/credentials/mac'))
  })

  it('does not revoke when the confirm is cancelled', async () => {
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke MacBook' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Cancel' }))
    await vi.waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(api.del).not.toHaveBeenCalled()
  })

  it('adds a passkey with a label and refreshes the list', async () => {
    mount()
    await screen.findByText('MacBook')
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'Phone' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add a passkey' }))
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith('/auth/webauthn/register/begin', { label: 'Phone' }))
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith(
      '/auth/webauthn/register/complete', expect.objectContaining({ ceremonyId: 'reg-1', label: 'Phone' })))
    await vi.waitFor(() =>
      expect(vi.mocked(api.get).mock.calls.filter(([p]) => p === '/auth/credentials')).toHaveLength(2))
  })

  it('shows a dismissed browser prompt as an inline error', async () => {
    create.mockRejectedValue(new DOMException('x', 'NotAllowedError'))
    mount()
    await screen.findByText('MacBook')
    fireEvent.click(screen.getByRole('button', { name: 'Add a passkey' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The passkey was not saved. The prompt was cancelled or timed out.')
  })

  it('asks a stale passkey session to sign in again before enrolling', async () => {
    const { ApiRequestError } = await import('@/api/client')
    vi.mocked(api.post).mockRejectedValueOnce(new ApiRequestError(403, {
      detail: { code: 'PASSKEY_REAUTH_REQUIRED', message: 'sign in again' },
    }))
    mount()
    await screen.findByText('MacBook')
    fireEvent.click(screen.getByRole('button', { name: 'Add a passkey' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Sign in again with your passkey, then add the new one.')
  })

  it('is a note, and fetches nothing, when passkeys are off', async () => {
    config = { repoRoot: '/r', adoptionEnabled: false, webauthnEnabled: false }
    mount()
    expect(await screen.findByText('Passkey sign-in is not turned on for this server.')).toBeInTheDocument()
    expect(api.get).not.toHaveBeenCalledWith('/auth/credentials')
  })

  it('is absent for a display-scoped session', async () => {
    me = { identity: 'display:Wall', scope: 'display', authMode: 'display' }
    const { container } = mount()
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledWith('/auth/me'))
    await vi.waitFor(() => expect(container).toBeEmptyDOMElement())
    expect(api.get).not.toHaveBeenCalledWith('/auth/credentials')
  })
})
