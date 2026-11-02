import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('sonner', () => ({ toast: Object.assign(vi.fn(), { error: vi.fn() }) }))
import { toast } from 'sonner'
import { MessageActions } from './MessageActions'
import type { Turn } from '@/session/reducer'

const turn: Turn = {
  id: 't', role: 'assistant',
  blocks: [{ kind: 'text', id: '1', text: 'a' }, { kind: 'text', id: '2', text: 'b' }],
}

function stubClipboard(write: () => Promise<void>) {
  const writeText = vi.fn(write)
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  return writeText
}

// Both tests replace these globals; put the originals back so later files in the
// same worker see the real jsdom surface.
const originalExecCommand = document.execCommand
const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')

describe('MessageActions', () => {
  beforeEach(() => vi.clearAllMocks())
  afterEach(() => {
    document.execCommand = originalExecCommand
    if (originalClipboard) Object.defineProperty(navigator, 'clipboard', originalClipboard)
    else Reflect.deleteProperty(navigator, 'clipboard')
  })

  it('copies the exact markdown and toasts Copied', async () => {
    const writeText = stubClipboard(async () => {})
    render(<MessageActions turn={turn} />)
    await userEvent.click(screen.getByRole('button', { name: /^Message actions/ }))
    await userEvent.click(screen.getByRole('menuitem', { name: 'Copy as Markdown' }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Copied'))
    expect(writeText).toHaveBeenCalledWith('a\n\nb')
  })

  it("toasts Couldn't copy when the write is rejected", async () => {
    stubClipboard(async () => { throw new Error('denied') })
    const exec = vi.fn(() => false)
    document.execCommand = exec as unknown as typeof document.execCommand
    render(<MessageActions turn={turn} />)
    await userEvent.click(screen.getByRole('button', { name: /^Message actions/ }))
    await userEvent.click(screen.getByRole('menuitem', { name: 'Copy as Markdown' }))
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Couldn't copy"))
    expect(toast).not.toHaveBeenCalled()
  })

  it('has no trigger when there is nothing to copy', () => {
    render(<MessageActions turn={{ id: 'u', role: 'user', blocks: [{ kind: 'image', id: 'i', images: [] }] }} />)
    expect(screen.queryByRole('button', { name: /^Message actions/ })).toBeNull()
  })
})
