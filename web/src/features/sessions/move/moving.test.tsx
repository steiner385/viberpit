/**
 * While a session is between hosts (D-17, `movingTo` set): the card shows the
 * "Moving to <host>…" chip, the composer's send button turns into a disabled
 * "Moving…" and sends nothing, and a 409 `SESSION_MOVING` from any action reads as
 * that same sentence rather than a raw error.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { ApiRequestError } from '@/api/client'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { sessionMovingMessage } from '@/api/sessionMove'
import type { Session } from '@/api/types'
import { session } from '@/test/factories'
import { ViewportProvider } from '@/shell/useViewport'
import { SessionCard } from '../list/SessionCard'
import { Composer } from '../composer/Composer'

let sessionsData: Session[] = []
vi.mock('@/api/hooks', () => ({ useSessions: () => ({ data: sessionsData }) }))

const noop = () => {}

function renderCard(s: Session) {
  return render(
    <ViewportProvider kiosk={null}>
      <MemoryRouter>
        <SessionCard session={s} account={undefined} onOpen={noop} onRename={noop}
          onClose={noop} onDelete={noop} onQuickResume={noop} />
      </MemoryRouter>
    </ViewportProvider>,
  )
}

describe('session card', () => {
  it('shows the chip while movingTo is set', () => {
    renderCard(session({ id: 'm1', host: 'dobby', movingTo: 'rocky' }))
    expect(screen.getByTestId('moving-chip')).toHaveTextContent('Moving to rocky…')
  })

  it('shows no chip otherwise', () => {
    renderCard(session({ id: 'm1', host: 'dobby', movingTo: null }))
    expect(screen.queryByTestId('moving-chip')).toBeNull()
  })

  it('renders an untrusted host name as text', () => {
    renderCard(session({ id: 'm1', movingTo: '<b>evil</b>' }))
    const chip = screen.getByTestId('moving-chip')
    expect(chip).toHaveTextContent('Moving to <b>evil</b>…')
    expect(chip.querySelector('b')).toBeNull()
  })
})

describe('composer', () => {
  const props = { status: { state: 'idle', count: 0 }, onPrompt: vi.fn(), onInterrupt: vi.fn(), sessionId: 'm1' }

  it('the send button is a disabled "Moving…" and Enter sends nothing', async () => {
    sessionsData = [session({ id: 'm1', movingTo: 'rocky' })]
    render(<Composer {...props} />)
    expect(screen.queryByRole('button', { name: 'Send message' })).toBeNull()
    const moving = screen.getByRole('button', { name: 'Moving…' })
    expect(moving).toBeDisabled()
    expect(moving).toHaveAttribute('title', 'Moving to rocky…')
    const box = screen.getByRole('textbox', { name: /message/i })
    await userEvent.type(box, 'hello{Enter}')
    expect(props.onPrompt).not.toHaveBeenCalled()
    expect(box).toHaveValue('hello')
  })

  it('sends normally once the move is over', () => {
    sessionsData = [session({ id: 'm1', movingTo: null })]
    render(<Composer {...props} />)
    expect(screen.getByRole('button', { name: 'Send message' })).toBeEnabled()
  })
})

describe('409 SESSION_MOVING from any action', () => {
  const err = new ApiRequestError(409, {
    detail: { code: 'SESSION_MOVING', message: 'This session is moving to rocky.', movingTo: 'rocky' },
  })

  it('reads as the chip sentence', () => {
    expect(sessionMovingMessage(err)).toBe('Moving to rocky…')
    expect(extractMutationErrorMessage(err)).toBe('Moving to rocky…')
  })

  it('leaves every other error alone', () => {
    const other = new ApiRequestError(409, { detail: { code: 'OTHER', message: 'nope' } })
    expect(sessionMovingMessage(other)).toBeNull()
    expect(extractMutationErrorMessage(other)).toBe('nope')
  })
})
