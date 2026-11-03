import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useParams } from 'react-router-dom'

vi.mock('sonner', () => ({ toast: Object.assign(vi.fn(), { error: vi.fn() }) }))
import { toast } from 'sonner'
import { MessageActions } from './MessageActions'
import type { Turn } from '@/session/reducer'
import { api, ApiRequestError } from '@/api/client'
import { ForkProvider } from '@/features/sessions/fork/ForkProvider'
import { storageKey } from '@/lib/storageKey'
import { REAUTH_MESSAGE } from '@/features/sessions/createSessionFailure'

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


// ── Fork from here (D-8) ─────────────────────────────────────────────────────────

const prompt1: Turn = { id: 'u1', role: 'user', blocks: [{ kind: 'text', id: 'u1:u', text: 'first ask' }] }
const reply1: Turn = { id: 't1', role: 'assistant', anchorUuid: 'anchor-1', blocks: [{ kind: 'text', id: 'x', text: 'answer' }] }
const prompt2: Turn = { id: 'u2', role: 'user', blocks: [{ kind: 'text', id: 'u2:u', text: 'second ask' }] }
const reply2: Turn = { id: 't2', role: 'assistant', blocks: [{ kind: 'text', id: 'y', text: 'interrupted' }] }
const TURNS = [prompt1, reply1, prompt2, reply2]

function OpenSession() {
  return <div>open session {useParams().id}</div>
}

/** The transcript host: SessionBody's provider, a router to land on the child. */
function host(ui: ReactNode, { enabled = true }: { enabled?: boolean } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/sessions/src']}>
        <Routes>
          <Route path="/sessions/src" element={
            <ForkProvider sessionId="src" turns={TURNS} earliestTurn={0} enabled={enabled}>{ui}</ForkProvider>
          } />
          <Route path="/sessions/:id" element={<OpenSession />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function chooseFork(name: RegExp) {
  await userEvent.click(screen.getByRole('button', { name }))
  await userEvent.click(screen.getByRole('menuitem', { name: 'Fork from here' }))
}

describe('MessageActions — Fork from here', () => {
  beforeEach(() => { vi.restoreAllMocks(); vi.clearAllMocks(); localStorage.clear() })

  it('forks a reply at its own turn, opens the child and says where its files come from', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ sessionId: 'child-1', prefill: null })
    host(<MessageActions turn={reply1} />)
    await chooseFork(/Message actions — reply/)
    await screen.findByText('open session child-1')
    expect(post).toHaveBeenCalledWith('/sessions/src/fork', {
      atTurnId: 't1', mode: 'reply', requestId: expect.stringMatching(/^[0-9a-f-]{36}$/),
    })
    // The copy is a lazy chunk: the toast follows the navigation by one import.
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Forked — files start from the current worktree, not this message'))
    expect(localStorage.getItem(storageKey('draft.child-1'))).toBeNull()
  })

  it('forks a prompt at the reply before it and seeds the child composer with the prefill', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ sessionId: 'child-2', prefill: 'second ask' })
    host(<MessageActions turn={prompt2} />)
    await chooseFork(/Message actions — your prompt/)
    await screen.findByText('open session child-2')
    expect(post).toHaveBeenCalledWith('/sessions/src/fork', expect.objectContaining({
      atTurnId: 't1', mode: 'message', prefill: 'second ask',
    }))
    expect(JSON.parse(localStorage.getItem(storageKey('draft.child-2')) ?? 'null'))
      .toEqual({ text: 'second ask', attachments: [] })
  })

  it('forks the first prompt with no cut', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ sessionId: 'child-3', prefill: 'first ask' })
    host(<MessageActions turn={prompt1} />)
    await chooseFork(/Message actions — your prompt/)
    await screen.findByText('open session child-3')
    expect(post).toHaveBeenCalledWith('/sessions/src/fork', expect.objectContaining({ atTurnId: null, mode: 'message' }))
  })

  it('offers no fork for a reply without an anchor', async () => {
    host(<MessageActions turn={reply2} />)
    await userEvent.click(screen.getByRole('button', { name: /Message actions — reply/ }))
    expect(screen.getByRole('menuitem', { name: 'Copy as Markdown' })).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: 'Fork from here' })).toBeNull()
  })

  it('offers no fork where the engine cannot fork, or outside a live session', async () => {
    host(<MessageActions turn={reply1} />, { enabled: false })
    await userEvent.click(screen.getByRole('button', { name: /Message actions — reply/ }))
    expect(screen.queryByRole('menuitem', { name: 'Fork from here' })).toBeNull()
  })

  it('offers no fork with no provider at all', async () => {
    render(<MessageActions turn={reply1} />)
    await userEvent.click(screen.getByRole('button', { name: /Message actions — reply/ }))
    expect(screen.queryByRole('menuitem', { name: 'Fork from here' })).toBeNull()
  })

  it('says why on a 409, and stays put', async () => {
    vi.spyOn(api, 'post').mockRejectedValue(new ApiRequestError(409, {
      detail: { code: 'FORK_OWNED_BY_AUTOMATION', message: 'server text' },
    }))
    host(<MessageActions turn={reply1} />)
    await chooseFork(/Message actions — reply/)
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(
      'An automation is running this session — fork it after the run finishes'))
    expect(toast).not.toHaveBeenCalled()
    expect(screen.queryByText(/open session/)).toBeNull()
  })

  it('says the shared no-usable-accounts line when every account needs a re-login', async () => {
    vi.spyOn(api, 'post').mockRejectedValue(new ApiRequestError(409, {
      error: { code: 'accounts_need_reauth', message: 'server text' },
    }))
    host(<MessageActions turn={reply1} />)
    await chooseFork(/Message actions — reply/)
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(REAUTH_MESSAGE))
  })

  it("falls back to the server's message for a code it does not map", async () => {
    vi.spyOn(api, 'post').mockRejectedValue(new ApiRequestError(409, { detail: 'model blocked by policy' }))
    host(<MessageActions turn={reply1} />)
    await chooseFork(/Message actions — reply/)
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('model blocked by policy'))
  })
})
