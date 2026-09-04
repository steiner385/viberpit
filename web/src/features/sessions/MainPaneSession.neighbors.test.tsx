/**
 * The session pane's prev/next chevrons.
 *
 * Was `components/session/__tests__/SessionView.neighbors.test.tsx`. `SessionView`
 * had no call site after Task 17 and is deleted; the chevrons were one of the
 * three things it owned that nothing in `AppShell` replaced, so they moved into
 * `MainPaneSession` — the one session pane, in every layout mode — rather than
 * being dropped with it. Every assertion below is the original.
 *
 * The one wording change: the touch target was `min-h-[44px]`, an arbitrary
 * value. `shell/**` bans those (`ccswitch/no-raw-palette`) and the rest of the
 * shell spells it `min-h-target` / `min-w-target`, which is the SAME 44px — see
 * `PhoneNav.test.tsx`'s "every control meets the 44px touch target".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { MainPaneSession } from './MainPaneSession'
import { SessionRuntimeProvider } from '@/components/session/SessionRuntimeProvider'
import { QK } from '@/api/hooks'
import type { Session } from '@/api/types'
import { mkSession } from '@/test/factories'

vi.mock('@/ws/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/ws/client')>()
  return {
    ...actual,
    useSessionSocket: () => ({
      attach: vi.fn(), detach: vi.fn(), interrupt: vi.fn(), prompt: vi.fn(),
      sendInterrupt: vi.fn(), permissionResponse: vi.fn(), answerQuestion: vi.fn(),
      setPermissionMode: vi.fn(), on: vi.fn(), off: vi.fn(),
      connectionState: 'connected' as const, socket: null,
    }),
  }
})

const mk = (id: string): Session =>
  mkSession(id, {
    accountName: 'a',
    createdAt: '2026-06-01T00:00:00Z',
    updatedAt: '2026-06-01T00:00:00Z',
  })

/**
 * The pane inside the runtime provider `PageHost` mounts for it in the app. The
 * pane READS the runtime now (audit F2.3) rather than creating one, so a test
 * that forgets the provider throws rather than quietly opening a second socket.
 */
function renderPane(sessionId: string, sessions: Session[], onOpenSession = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(QK.sessions, sessions)
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <SessionRuntimeProvider sessionId={sessionId}>{children}</SessionRuntimeProvider>
    </QueryClientProvider>
  )
  render(
    <MainPaneSession
      sessionId={sessionId}
      onClosed={vi.fn()}
      onDetach={vi.fn()}
      onOpenSession={onOpenSession}
    />,
    { wrapper },
  )
  return { onOpenSession }
}

describe('MainPaneSession prev/next chevrons', () => {
  beforeEach(() => window.localStorage.clear())

  it('renders prev/next chevrons with 44px touch targets when neighbors exist', () => {
    renderPane('b', [mk('a'), mk('b'), mk('c')])
    const next = screen.getByRole('button', { name: /next session/i })
    expect(screen.getByRole('button', { name: /previous session/i })).toBeInTheDocument()
    expect(next.className).toMatch(/min-h-target/)
    expect(next.className).toMatch(/min-w-target/)
  })

  it('navigates to the next session id when › is clicked', async () => {
    const user = userEvent.setup()
    const { onOpenSession } = renderPane('b', [mk('a'), mk('b'), mk('c')])
    await user.click(screen.getByRole('button', { name: /next session/i }))
    expect(onOpenSession).toHaveBeenCalledWith('c')
  })

  it('wraps to the first session when › is clicked on the last session', async () => {
    const user = userEvent.setup()
    const { onOpenSession } = renderPane('c', [mk('a'), mk('b'), mk('c')])
    await user.click(screen.getByRole('button', { name: /next session/i }))
    expect(onOpenSession).toHaveBeenCalledWith('a')
  })

  it('hides the chevrons when there is only one session', () => {
    renderPane('a', [mk('a')])
    expect(screen.queryByRole('button', { name: /next session/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /previous session/i })).not.toBeInTheDocument()
  })
})
