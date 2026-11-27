import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { bytesToBase64url } from '@/lib/base64url'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() },
  }
})

import { api } from '@/api/client'
import { enrolPasskey, passkeysSupported, startPasskeyLogin } from '../webauthn'

const post = vi.mocked(api.post)
const bytes = (...xs: number[]) => new Uint8Array(xs).buffer

const get = vi.fn()
const create = vi.fn()

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('PublicKeyCredential', class {})
  Object.defineProperty(navigator, 'credentials', { configurable: true, value: { get, create } })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('startPasskeyLogin', () => {
  beforeEach(() => {
    post.mockImplementation(async (path: string) => {
      if (path === '/auth/webauthn/login/begin') {
        return {
          ceremonyId: 'cer-1',
          publicKey: {
            challenge: bytesToBase64url(bytes(1, 2, 3)),
            rpId: 'pit.example',
            timeout: 60000,
            userVerification: 'required',
            allowCredentials: [{ type: 'public-key', id: bytesToBase64url(bytes(9, 9)), transports: ['internal'] }],
          },
        }
      }
      return { identity: 'alice@example.com', authMode: 'passkey' }
    })
    get.mockResolvedValue({
      id: '-_8', rawId: bytes(0xfb, 0xff), type: 'public-key', authenticatorAttachment: 'platform',
      response: {
        clientDataJSON: bytes(1), authenticatorData: bytes(2), signature: bytes(3), userHandle: bytes(4),
      },
      getClientExtensionResults: () => ({}),
    })
  })

  it('runs begin → navigator.credentials.get → complete with base64url JSON', async () => {
    const out = await startPasskeyLogin('alice@example.com')

    expect(post).toHaveBeenNthCalledWith(1, '/auth/webauthn/login/begin', { identity: 'alice@example.com' })
    const opts = get.mock.calls[0][0] as CredentialRequestOptions
    const pk = opts.publicKey!
    expect(Array.from(new Uint8Array(pk.challenge as ArrayBuffer))).toEqual([1, 2, 3])
    expect(pk.rpId).toBe('pit.example')
    expect(pk.userVerification).toBe('required')
    expect(Array.from(new Uint8Array(pk.allowCredentials![0].id as ArrayBuffer))).toEqual([9, 9])

    expect(post).toHaveBeenNthCalledWith(2, '/auth/webauthn/login/complete', {
      ceremonyId: 'cer-1',
      credential: {
        id: '-_8', rawId: '-_8', type: 'public-key', authenticatorAttachment: 'platform',
        response: { clientDataJSON: 'AQ', authenticatorData: 'Ag', signature: 'Aw', userHandle: 'BA' },
        clientExtensionResults: {},
      },
    })
    expect(out).toEqual({ identity: 'alice@example.com', authMode: 'passkey' })
  })

  it('omits identity for a discoverable (usernameless) login', async () => {
    await startPasskeyLogin('  ')
    expect(post).toHaveBeenNthCalledWith(1, '/auth/webauthn/login/begin', {})
  })

  it('omits a null userHandle', async () => {
    get.mockResolvedValueOnce({
      id: 'AA', rawId: bytes(0), type: 'public-key', authenticatorAttachment: null,
      response: { clientDataJSON: bytes(1), authenticatorData: bytes(2), signature: bytes(3), userHandle: null },
      getClientExtensionResults: () => ({}),
    })
    await startPasskeyLogin()
    const body = post.mock.calls[1][1] as { credential: { response: Record<string, unknown> } & Record<string, unknown> }
    expect(body.credential.response).not.toHaveProperty('userHandle')
    expect(body.credential).not.toHaveProperty('authenticatorAttachment')
  })

  it('does not call complete when the browser prompt is dismissed', async () => {
    get.mockRejectedValueOnce(new DOMException('cancelled', 'NotAllowedError'))
    await expect(startPasskeyLogin()).rejects.toMatchObject({ name: 'NotAllowedError' })
    expect(post).toHaveBeenCalledTimes(1)
  })
})

describe('enrolPasskey', () => {
  beforeEach(() => {
    post.mockImplementation(async (path: string) => {
      if (path === '/auth/webauthn/register/begin') {
        return {
          ceremonyId: 'reg-1',
          publicKey: {
            challenge: bytesToBase64url(bytes(5)),
            rp: { id: 'pit.example', name: 'ViberPit' },
            user: { id: bytesToBase64url(bytes(7, 7)), name: 'alice@example.com', displayName: 'alice' },
            pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
            excludeCredentials: [{ type: 'public-key', id: bytesToBase64url(bytes(8)) }],
            authenticatorSelection: { residentKey: 'required', userVerification: 'required' },
            attestation: 'none',
          },
        }
      }
      return { id: 'cred-1', label: 'Laptop', transports: ['internal'], createdAt: 'x', lastUsedAt: null }
    })
    create.mockResolvedValue({
      id: 'AQ', rawId: bytes(1), type: 'public-key', authenticatorAttachment: 'platform',
      response: { clientDataJSON: bytes(2), attestationObject: bytes(3), getTransports: () => ['internal', 'hybrid'] },
      getClientExtensionResults: () => ({}),
    })
  })

  it('runs begin → navigator.credentials.create → complete with base64url JSON', async () => {
    const out = await enrolPasskey(' Laptop ')

    expect(post).toHaveBeenNthCalledWith(1, '/auth/webauthn/register/begin', { label: 'Laptop' })
    const pk = (create.mock.calls[0][0] as CredentialCreationOptions).publicKey!
    expect(Array.from(new Uint8Array(pk.challenge as ArrayBuffer))).toEqual([5])
    expect(Array.from(new Uint8Array(pk.user.id as ArrayBuffer))).toEqual([7, 7])
    expect(pk.user.name).toBe('alice@example.com')
    expect(pk.rp).toEqual({ id: 'pit.example', name: 'ViberPit' })
    expect(Array.from(new Uint8Array(pk.excludeCredentials![0].id as ArrayBuffer))).toEqual([8])

    expect(post).toHaveBeenNthCalledWith(2, '/auth/webauthn/register/complete', {
      ceremonyId: 'reg-1',
      label: 'Laptop',
      credential: {
        id: 'AQ', rawId: 'AQ', type: 'public-key', authenticatorAttachment: 'platform',
        response: { clientDataJSON: 'Ag', attestationObject: 'Aw', transports: ['internal', 'hybrid'] },
        clientExtensionResults: {},
      },
    })
    expect(out.id).toBe('cred-1')
  })

  it('sends no label when none is given', async () => {
    await enrolPasskey()
    expect(post).toHaveBeenNthCalledWith(1, '/auth/webauthn/register/begin', {})
    expect(post.mock.calls[1][1]).not.toHaveProperty('label')
  })
})

describe('passkeysSupported', () => {
  it('is false without the WebAuthn API', () => {
    vi.stubGlobal('PublicKeyCredential', undefined)
    expect(passkeysSupported()).toBe(false)
  })

  it('is true with it', () => {
    expect(passkeysSupported()).toBe(true)
  })
})
