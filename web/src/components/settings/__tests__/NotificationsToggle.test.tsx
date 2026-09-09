import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { NotificationsToggle } from '../NotificationsToggle'
import { BRAND_NAME } from '@/brand'

vi.mock('@/api/client', () => ({
  api: {
    get: vi.fn(async () => ({ vapidPublicKey: 'BPUB', enabled: true })),
    post: vi.fn(async () => ({ ok: true })),
    del: vi.fn(async () => ({ ok: true })),
  },
}))

vi.mock('@/api/push', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/push')>()),
  installState: vi.fn(() => 'not-required' as const),
}))

describe('NotificationsToggle', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    const { installState } = await import('@/api/push')
    vi.mocked(installState).mockReturnValue('not-required')
  })

  it('shows unsupported when the browser lacks Push', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    delete (globalThis as any).PushManager
    render(<NotificationsToggle />)
    expect(screen.getByText(/not supported/i)).toBeInTheDocument()
  })

  it('subscribes on enable when supported and permission granted', async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(globalThis as any).PushManager = function () {}
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(globalThis as any).Notification = { requestPermission: vi.fn(async () => 'granted'), permission: 'default' }
    const subscribe = vi.fn(async () => ({
      toJSON: () => ({ endpoint: 'https://p/1', keys: { p256dh: 'k', auth: 'a' } }),
    }))
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(navigator as any).serviceWorker = {
      register: vi.fn(async () => ({ pushManager: { getSubscription: vi.fn(async () => null), subscribe } })),
      ready: Promise.resolve({ pushManager: { getSubscription: vi.fn(async () => null), subscribe } }),
    }
    const { api } = await import('@/api/client')
    render(<NotificationsToggle />)
    fireEvent.click(await screen.findByRole('button', { name: /enable/i }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/push/subscribe', expect.objectContaining({ endpoint: 'https://p/1' })))
  })

  // NOTE: assert against `container.textContent`, not `getByText`. The copy
  // interpolates {BRAND_NAME}, which splits "Open ViberPit from your Home
  // Screen" across three text nodes — getByText matches per-node and would miss
  // it, while /home screen/i would match two elements and throw.

  it('offers install instructions, not a dead control, for iOS in a tab', async () => {
    const { installState } = await import('@/api/push')
    vi.mocked(installState).mockReturnValue('needs-install')
    const { container } = render(<NotificationsToggle />)
    expect(container.textContent).toMatch(/add to home screen/i)
    expect(screen.queryByRole('button', { name: /enable/i })).not.toBeInTheDocument()
    expect(container.textContent).not.toMatch(/not supported/i)
  })

  it('states the required order: install, reopen, then enable', async () => {
    const { installState } = await import('@/api/push')
    vi.mocked(installState).mockReturnValue('needs-install')
    const { container } = render(<NotificationsToggle />)
    // Enabling in the Safari tab does not carry over to the installed app, so
    // the copy has to say so — this is the whole point of the branch.
    // Built from BRAND_NAME, not the literal — the brand is spelled in one
    // place, and that rule holds in tests too.
    expect(container.textContent).toMatch(
      new RegExp(`open ${BRAND_NAME} from your home screen`, 'i'),
    )
    expect(container.textContent).toMatch(/order matters/i)
  })
})
