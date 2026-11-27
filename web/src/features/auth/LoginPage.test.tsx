import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ApiRequestError } from '@/api/client'

const startPasskeyLogin = vi.fn()
let supported = true
vi.mock('@/api/webauthn', () => ({
  startPasskeyLogin: (identity?: string) => startPasskeyLogin(identity),
  passkeysSupported: () => supported,
}))

import { LoginPage } from './LoginPage'

const replace = vi.fn()

function mount(search = '') {
  return render(
    <MemoryRouter initialEntries={[`/login${search}`]}>
      <LoginPage />
    </MemoryRouter>,
  )
}

const signIn = () => fireEvent.click(screen.getByRole('button', { name: 'Sign in with a passkey' }))

beforeEach(() => {
  startPasskeyLogin.mockReset()
  replace.mockReset()
  supported = true
  vi.stubGlobal('location', { ...window.location, replace })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('LoginPage (#1410 FR-G12.12)', () => {
  it('signs in with a discoverable passkey and lands on /', async () => {
    startPasskeyLogin.mockResolvedValue({ identity: 'alice@example.com', authMode: 'passkey' })
    mount()
    signIn()
    await vi.waitFor(() => expect(replace).toHaveBeenCalledWith('/'))
    expect(startPasskeyLogin).toHaveBeenCalledWith(undefined)
  })

  it('passes the optional identity and follows a same-origin `next`', async () => {
    startPasskeyLogin.mockResolvedValue({ identity: 'alice@example.com', authMode: 'passkey' })
    mount('?next=%2Fpipeline%3Fx%3D1')
    fireEvent.change(screen.getByLabelText(/Email or username/), { target: { value: 'alice@example.com' } })
    signIn()
    await vi.waitFor(() => expect(replace).toHaveBeenCalledWith('/pipeline?x=1'))
    expect(startPasskeyLogin).toHaveBeenCalledWith('alice@example.com')
  })

  it('ignores an off-site `next`', async () => {
    startPasskeyLogin.mockResolvedValue({ identity: 'a', authMode: 'passkey' })
    mount('?next=https%3A%2F%2Fevil.example%2F')
    signIn()
    await vi.waitFor(() => expect(replace).toHaveBeenCalledWith('/'))
  })

  it('shows a refused passkey inline and stays put', async () => {
    startPasskeyLogin.mockRejectedValue(new ApiRequestError(401, { error: { code: 'UNAUTHORIZED' } }))
    mount()
    signIn()
    expect(await screen.findByRole('alert')).toHaveTextContent('That passkey was not accepted')
    expect(replace).not.toHaveBeenCalled()
  })

  it('shows the Retry-After wait on a 429', async () => {
    startPasskeyLogin.mockRejectedValue(new ApiRequestError(429, {}, 42))
    mount()
    signIn()
    expect(await screen.findByRole('alert')).toHaveTextContent('Too many attempts. Try again in 42 seconds.')
  })

  it('says so when the browser prompt was dismissed', async () => {
    startPasskeyLogin.mockRejectedValue(new DOMException('x', 'NotAllowedError'))
    mount()
    signIn()
    expect(await screen.findByRole('alert')).toHaveTextContent('Sign-in was cancelled or timed out.')
  })

  it('disables the button while the ceremony runs', async () => {
    startPasskeyLogin.mockReturnValue(new Promise(() => {}))
    mount()
    signIn()
    expect(await screen.findByRole('button', { name: /Sign in with a passkey/ })).toBeDisabled()
  })

  it('explains when the browser has no passkey support', () => {
    supported = false
    mount()
    expect(screen.getByRole('button', { name: 'Sign in with a passkey' })).toBeDisabled()
    expect(screen.getByText('This browser does not support passkeys.')).toBeInTheDocument()
  })
})
