import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('sonner', () => ({ toast: Object.assign(vi.fn(), { error: vi.fn() }) }))
import { toast } from 'sonner'
import { api, ApiRequestError } from '@/api/client'
import type { RewindResult } from '@/api/hooks'
import type { Turn } from '@/session/reducer'
import { SessionGestureProvider } from '@/shell/sessionGestures'
import { RewindProvider } from '@/features/sessions/rewind/RewindProvider'
import { ErrorCard } from './ErrorCard'

describe('ErrorCard', () => {
  it('renders the error message inline as an alert', () => {
    render(<ErrorCard code="max_turns" message="Reached the maximum number of turns" />)
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('Reached the maximum number of turns')
  })

  it('shows the error code so the failure is identifiable', () => {
    render(<ErrorCard code="api_429" message="rate limited" />)
    expect(screen.getByText(/api_429/)).toBeInTheDocument()
  })

  it('shows a reset time when provided', () => {
    render(<ErrorCard code="rate_limit" message="slow down" resetsAt="2024-01-01T00:00:00Z" />)
    expect(screen.getByText(/Resets at/i)).toBeInTheDocument()
  })
})

// ── Rewind & retry (D-9) ─────────────────────────────────────────────────────

const UUID = '0b9c7a52-6f1e-4c55-9d1e-2f3a4b5c6d7e'
const prompt: Turn = { id: UUID, role: 'user', blocks: [{ kind: 'text', id: 'p:u', text: 'fix the bug' }] }
const PREVIEW = { files: ['src/a.ts'], deleted: [], insertions: 2, deletions: 1, checkpointAvailable: true }
const RESULT: RewindResult = { rewindId: 'r1', prefill: 'fix the bug (as the CLI kept it)', filesRestored: ['src/a.ts'] }
const RETRY = 'Rewind & retry'

/** Answers the dry run with `preview` and the rewind with `result` (or throws the given error). */
function mockApi({ preview = PREVIEW as unknown, result = RESULT as unknown } = {}) {
  return vi.spyOn(api, 'post').mockImplementation(async (_url: string, body?: unknown) => {
    const answer = (body as { dryRun?: boolean }).dryRun ? preview : result
    if (answer instanceof Error) throw answer
    return answer
  })
}

const send = vi.fn()
const RETRY_TITLE = 'Rewind and retry this message?'
const RETRY_NOTE = "Your message is sent again right after the rewind, so this rewind can't be undone."

/** `applied`: the rewindId whose `rewound` the reducer has applied (the transcript is cut). */
function tree(ui: ReactNode, qc: QueryClient, { enabled = true, status = 'idle', applied = 'r1' as string | null } = {}) {
  return (
    <QueryClientProvider client={qc}>
      <SessionGestureProvider>
        <RewindProvider sessionId="s1" enabled={enabled} status={status} send={send} appliedRewindId={applied}>
          {ui}
        </RewindProvider>
      </SessionGestureProvider>
    </QueryClientProvider>
  )
}

function host(ui: ReactNode, opts: { enabled?: boolean; status?: string; applied?: string | null } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const r = render(tree(ui, qc, opts))
  return { ...r, rerenderWith: (o: typeof opts) => r.rerender(tree(ui, qc, { ...opts, ...o })) }
}

const card = (p: Turn | null = prompt) => <ErrorCard code="api_error" message="Overloaded" prompt={p} />

async function confirmRetry() {
  await userEvent.click(screen.getByRole('button', { name: RETRY }))
  await screen.findByRole('dialog', { name: RETRY_TITLE })
  await screen.findByText('src/a.ts')
  await userEvent.click(screen.getByRole('button', { name: 'Rewind and retry' }))
}

describe('ErrorCard — Rewind & retry', () => {
  beforeEach(() => { vi.restoreAllMocks(); vi.clearAllMocks() })

  it('is offered for a failed prompt with a target while the session can rewind and is idle', () => {
    host(card())
    expect(screen.getByRole('button', { name: RETRY })).toBeInTheDocument()
  })

  it('is absent without the capability', () => {
    host(card(), { enabled: false })
    expect(screen.queryByRole('button', { name: RETRY })).toBeNull()
  })

  it('is absent outside a rewind provider (the archived reader)', () => {
    render(card())
    expect(screen.queryByRole('button', { name: RETRY })).toBeNull()
  })

  it('is absent when the failed turn has no prompt of its own', () => {
    host(card(null))
    expect(screen.queryByRole('button', { name: RETRY })).toBeNull()
  })

  it('is absent when the prompt has no rewind target yet (a live prompt with no uuid echo)', () => {
    host(card({ ...prompt, id: '9f8e7d6c5b4a39281706f5e4d3c2b1a0' }))
    expect(screen.queryByRole('button', { name: RETRY })).toBeNull()
  })

  it('is absent when the prompt carried images — a resend would drop them', () => {
    host(card({ ...prompt, blocks: [...prompt.blocks,
      { kind: 'image', id: 'p:img', images: [{ url: '/a.png', mime: 'image/png', name: 'a.png' }] }] }))
    expect(screen.queryByRole('button', { name: RETRY })).toBeNull()
  })

  it('is absent when the prompt has no text', () => {
    host(card({ ...prompt, blocks: [] }))
    expect(screen.queryByRole('button', { name: RETRY })).toBeNull()
  })

  it('is absent for a nudge prompt (autopilot or completion)', () => {
    host(card({ ...prompt, origin: { kind: 'completion' } }))
    expect(screen.queryByRole('button', { name: RETRY })).toBeNull()
  })

  it.each(['in_progress', 'waiting_input', 'waiting_question', 'ready_review'])(
    'is absent while the server would refuse a rewind (%s)', (status) => {
      host(card(), { status })
      expect(screen.queryByRole('button', { name: RETRY })).toBeNull()
    })

  it('says the prompt is sent again and that the rewind cannot be undone after', async () => {
    mockApi()
    host(card())
    await userEvent.click(screen.getByRole('button', { name: RETRY }))
    const dialog = await screen.findByRole('dialog', { name: RETRY_TITLE })
    expect(dialog.textContent).toContain(RETRY_NOTE)
    expect(screen.getByRole('button', { name: 'Rewind and retry' })).toBeInTheDocument()
  })

  it('is disabled while a rewind dialog is open', async () => {
    mockApi()
    host(<>{card()}{card()}</>)
    const [a, b] = screen.getAllByRole('button', { name: RETRY })
    await userEvent.click(a)
    await screen.findByRole('dialog', { name: RETRY_TITLE })
    expect(a).toBeDisabled()
    expect(b).toBeDisabled()
  })

  it('confirms, rewinds to the prompt, then sends the prefill unchanged exactly once', async () => {
    const post = mockApi()
    host(card())
    await confirmRetry()
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1))
    expect(send).toHaveBeenCalledWith(RESULT.prefill, [])
    expect(post).toHaveBeenLastCalledWith('/sessions/s1/rewind', expect.objectContaining({
      toTurnId: UUID, mode: 'files_and_conversation', dryRun: false }))
  })

  it('waits for the transcript cut (the matching rewound) before sending', async () => {
    mockApi()
    const h = host(card(), { applied: null })
    await confirmRetry()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await new Promise((r) => setTimeout(r, 50))
    expect(send).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: RETRY })).toBeDisabled()
    h.rerenderWith({ applied: 'other' })
    await new Promise((r) => setTimeout(r, 20))
    expect(send).not.toHaveBeenCalled()
    h.rerenderWith({ applied: 'r1' })
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1))
    expect(send).toHaveBeenCalledWith(RESULT.prefill, [])
  })

  it('sends anyway after a bounded wait when the cut never shows', async () => {
    mockApi()
    host(card(), { applied: null })
    await confirmRetry()
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1), { timeout: 4000 })
  })

  it("sends the failed prompt's own text when the rewind returns no prefill", async () => {
    mockApi({ result: { ...RESULT, prefill: null } })
    host(card())
    await confirmRetry()
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1))
    expect(send).toHaveBeenCalledWith('fix the bug', [])
  })

  it('a refused rewind sends nothing and says why', async () => {
    mockApi({ result: new ApiRequestError(409, { detail: { code: 'REWIND_SPLITS_TOOL_CALL', message: 'server text' } }) })
    host(card())
    await confirmRetry()
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(
      "Can't rewind here — it would split a tool call from its result"))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(send).not.toHaveBeenCalled()
  })

  it('a refused dry run sends nothing and says why', async () => {
    mockApi({ preview: new ApiRequestError(409, { detail: { code: 'REWIND_BUSY', message: 'busy' } }) })
    host(card())
    await userEvent.click(screen.getByRole('button', { name: RETRY }))
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(
      "Claude is working or waiting on you — rewind once that's settled"))
    expect(send).not.toHaveBeenCalled()
  })

  it('cancel sends nothing and rewinds nothing', async () => {
    const post = mockApi()
    host(card())
    await userEvent.click(screen.getByRole('button', { name: RETRY }))
    await screen.findByText('src/a.ts')
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(post).toHaveBeenCalledTimes(1)
    expect(send).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: RETRY })).not.toBeDisabled()
  })
})
