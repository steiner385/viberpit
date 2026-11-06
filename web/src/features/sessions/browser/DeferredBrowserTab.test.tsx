import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { DeferredBrowserTab } from './DeferredBrowserTab'

// The viewer's per-session state stands in for all of it (preview open, notices,
// the no-display backoff): a seam that holds one piece of local state.
vi.mock('./BrowserTab', async () => {
  const { useState } = await import('react')
  return {
    BrowserTab: ({ sessionId }: { sessionId: string }) => {
      const [opened, setOpened] = useState(false)
      return (
        <button type="button" onClick={() => setOpened(true)}>
          {sessionId}:{opened ? 'preview open' : 'no preview'}
        </button>
      )
    },
  }
})

describe('DeferredBrowserTab', () => {
  it("keys the viewer by session: another session never inherits this one's state", async () => {
    const { rerender } = render(<DeferredBrowserTab sessionId="A" statusState="idle" />)
    fireEvent.click(await screen.findByRole('button', { name: 'A:no preview' }))
    expect(screen.getByRole('button', { name: 'A:preview open' })).toBeInTheDocument()
    rerender(<DeferredBrowserTab sessionId="B" statusState="idle" />)
    expect(await screen.findByRole('button', { name: 'B:no preview' })).toBeInTheDocument()
  })
})
