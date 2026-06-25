import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { NotificationsToggle } from '../NotificationsToggle'

vi.mock('@/api/client', () => ({
  api: {
    get: vi.fn(async () => ({ vapidPublicKey: 'BPUB', enabled: true })),
    post: vi.fn(async () => ({ ok: true })),
    del: vi.fn(async () => ({ ok: true })),
  },
}))

describe('NotificationsToggle', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('shows unsupported when the browser lacks Push', () => {
    // @ts-expect-error force-unset
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
})
