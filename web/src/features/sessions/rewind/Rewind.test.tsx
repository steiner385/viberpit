/**
 * Rewind to here / Edit & resend (D-9) from the message menu: the confirm is built from
 * the dry run (files, deletions marked, `+ins −del`, the caveat — or the conversation-only
 * line, which sends `mode: "conversation"`), confirm rewinds, Edit & resend refills this
 * session's composer, a 409 says why, and the items are disabled while busy and absent
 * without the capability. The divider's Undo posts the rewindId and lists skipped paths.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useEffect, type ReactNode } from 'react'
import { render, renderHook, screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('sonner', () => ({ toast: Object.assign(vi.fn(), { error: vi.fn() }) }))
import { toast } from 'sonner'
import { api, ApiRequestError } from '@/api/client'
import type { RewindResult } from '@/api/hooks'
import type { Turn } from '@/session/reducer'
import { storageKey } from '@/lib/storageKey'
import { SessionGestureProvider, useComposerAttachRegistration } from '@/shell/sessionGestures'
import { MessageActions } from '@/features/sessions/transcript/MessageActions'
import { RewindProvider } from './RewindProvider'
import { RewindDividerSlot } from './RewindDividerSlot'
import { useRewind } from './useRewind'

const UUID = '0b9c7a52-6f1e-4c55-9d1e-2f3a4b5c6d7e'
const prompt: Turn = { id: UUID, role: 'user', blocks: [{ kind: 'text', id: 'p:u', text: 'fix the bug' }] }
const PREVIEW = { files: ['src/a.ts', 'src/new.ts'], deleted: ['src/new.ts'], insertions: 12, deletions: 3, checkpointAvailable: true }
const RESULT: RewindResult = { rewindId: 'r1', prefill: 'fix the bug', filesRestored: ['src/a.ts', 'src/new.ts'] }
const CAVEAT = 'Changes made by shell commands are not restored, and edits made outside Claude since that message are overwritten.'
const CONVERSATION_ONLY = "Files can't be restored for this message — only the conversation will be rewound."

/** Answers the dry run with `preview` and the rewind with `result` (or the given errors). */
function mockApi({ preview = PREVIEW as unknown, result = RESULT as unknown }: { preview?: unknown; result?: unknown } = {}) {
  return vi.spyOn(api, 'post').mockImplementation(async (_url: string, body?: unknown) => {
    const answer = (body as { dryRun?: boolean }).dryRun ? preview : result
    if (answer instanceof Error) throw answer
    return answer
  })
}

const setDraft = vi.fn()
/** A stand-in dock composer registered on the shell's bridge. */
function FakeComposer() {
  const ref = useComposerAttachRegistration()
  useEffect(() => { if (ref) ref.current = { attachFile: vi.fn(), setDraft } })
  return null
}

function host(ui: ReactNode, { enabled = true, busy = false, composer = false } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <SessionGestureProvider>
        <RewindProvider sessionId="s1" enabled={enabled} busy={busy}>{ui}</RewindProvider>
        {composer && <FakeComposer />}
      </SessionGestureProvider>
    </QueryClientProvider>,
  )
}

async function choose(item: 'Rewind to here' | 'Edit & resend') {
  await userEvent.click(screen.getByRole('button', { name: /Message actions — your prompt/ }))
  await userEvent.click(screen.getByRole('menuitem', { name: item }))
}

describe('Rewind to here', () => {
  beforeEach(() => { vi.restoreAllMocks(); vi.clearAllMocks(); localStorage.clear() })

  it('confirms with the dry run: files, deletions marked, + and −, and the caveat', async () => {
    const post = mockApi()
    host(<MessageActions turn={prompt} />)
    await choose('Rewind to here')
    const dialog = await screen.findByRole('dialog', { name: 'Rewind to this message?' })
    expect(await screen.findByText('src/a.ts')).toBeTruthy()
    expect(screen.getByText('src/new.ts').textContent).toBe('src/new.ts(deleted)')
    expect(screen.getByText('+12')).toBeTruthy()
    expect(screen.getByText('−3')).toBeTruthy()
    expect(dialog.textContent).toContain(CAVEAT)
    expect(post).toHaveBeenCalledWith('/sessions/s1/rewind', expect.objectContaining({ toTurnId: UUID, dryRun: true }))

    await userEvent.click(screen.getByRole('button', { name: 'Rewind' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(post).toHaveBeenLastCalledWith('/sessions/s1/rewind', expect.objectContaining({
      toTurnId: UUID, mode: 'files_and_conversation', dryRun: false }))
    expect(localStorage.getItem(storageKey('draft.s1'))).toBeNull()
  })

  it('without a checkpoint, says only the conversation rewinds and sends mode "conversation"', async () => {
    const post = mockApi({ preview: { files: [], deleted: [], insertions: 0, deletions: 0, checkpointAvailable: false } })
    host(<MessageActions turn={prompt} />)
    await choose('Rewind to here')
    expect(await screen.findByText(CONVERSATION_ONLY)).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Rewind' }))
    await waitFor(() => expect(post).toHaveBeenLastCalledWith('/sessions/s1/rewind', expect.objectContaining({
      mode: 'conversation', dryRun: false })))
  })

  it('cancel sends nothing past the dry run', async () => {
    const post = mockApi()
    host(<MessageActions turn={prompt} />)
    await choose('Rewind to here')
    await screen.findByText('src/a.ts')
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(post).toHaveBeenCalledTimes(1)
  })

  it('says why on a 409 from the dry run, with the mapped copy', async () => {
    mockApi({ preview: new ApiRequestError(409, { detail: { code: 'REWIND_SPLITS_TOOL_CALL', message: 'server text' } }) })
    host(<MessageActions turn={prompt} />)
    await choose('Rewind to here')
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(
      "Can't rewind here — it would split a tool call from its result"))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('says why on a 409 from the rewind itself', async () => {
    mockApi({ result: new ApiRequestError(409, { detail: { code: 'REWIND_TOO_LARGE', message: 'server text' } }) })
    host(<MessageActions turn={prompt} />)
    await choose('Rewind to here')
    await screen.findByText('src/a.ts')
    await userEvent.click(screen.getByRole('button', { name: 'Rewind' }))
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(
      'Too many changed files to back up for undo, so nothing was rewound'))
  })

  it("falls back to the server's message for a code it does not map", async () => {
    mockApi({ preview: new ApiRequestError(409, { detail: { code: 'SOMETHING_NEW', message: 'server text' } }) })
    host(<MessageActions turn={prompt} />)
    await choose('Rewind to here')
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('server text'))
  })
})

describe('Edit & resend', () => {
  beforeEach(() => { vi.restoreAllMocks(); vi.clearAllMocks(); localStorage.clear() })

  it('rewinds, then fills the mounted composer with the prefill', async () => {
    mockApi()
    host(<MessageActions turn={prompt} />, { composer: true })
    await choose('Edit & resend')
    expect(await screen.findByRole('dialog', { name: 'Rewind and edit this message?' })).toBeTruthy()
    await screen.findByText('src/a.ts')
    await userEvent.click(screen.getByRole('button', { name: 'Rewind and edit' }))
    await waitFor(() => expect(setDraft).toHaveBeenCalledWith('fix the bug'))
  })

  it('with no composer mounted, saves the prefill as the session draft', async () => {
    mockApi()
    host(<MessageActions turn={prompt} />)
    await choose('Edit & resend')
    await screen.findByText('src/a.ts')
    await userEvent.click(screen.getByRole('button', { name: 'Rewind and edit' }))
    await waitFor(() => expect(JSON.parse(localStorage.getItem(storageKey('draft.s1')) ?? 'null'))
      .toEqual({ text: 'fix the bug', attachments: [] }))
  })
})

describe('gating', () => {
  beforeEach(() => { vi.restoreAllMocks(); vi.clearAllMocks() })

  it('disables both items while a turn runs', async () => {
    host(<MessageActions turn={prompt} />, { busy: true })
    await userEvent.click(screen.getByRole('button', { name: /Message actions — your prompt/ }))
    expect(screen.getByRole('menuitem', { name: 'Rewind to here' }).getAttribute('aria-disabled')).toBe('true')
    expect(screen.getByRole('menuitem', { name: 'Edit & resend' }).getAttribute('aria-disabled')).toBe('true')
  })

  it('hides both items without the capability, or outside a live session', async () => {
    host(<MessageActions turn={prompt} />, { enabled: false })
    await userEvent.click(screen.getByRole('button', { name: /Message actions — your prompt/ }))
    expect(screen.getByRole('menuitem', { name: 'Copy as Markdown' })).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: 'Rewind to here' })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: 'Edit & resend' })).toBeNull()
  })

  it('offers nothing on a prompt with no target (no CLI uuid yet)', async () => {
    host(<MessageActions turn={{ ...prompt, id: 'livehexid' }} />)
    await userEvent.click(screen.getByRole('button', { name: /Message actions — your prompt/ }))
    expect(screen.queryByRole('menuitem', { name: 'Rewind to here' })).toBeNull()
  })
})

describe('the rewind divider', () => {
  beforeEach(() => { vi.restoreAllMocks(); vi.clearAllMocks() })
  const marker = { rewindId: 'r1', filesRestored: ['src/a.ts', 'src/new.ts'] }

  it('says how many files came back, and Undo posts the rewindId', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ ok: true, skipped: [] })
    host(<RewindDividerSlot marker={marker} />)
    expect(await screen.findByText('Rewound — 2 files restored')).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Undo rewind' }))
    expect(post).toHaveBeenCalledWith('/sessions/s1/rewind/undo', { rewindId: 'r1' })
    expect(toast).not.toHaveBeenCalled()
  })

  it('lists the paths an undo left alone', async () => {
    vi.spyOn(api, 'post').mockResolvedValue({ ok: true, skipped: ['link/a.txt', 'b.txt'] })
    host(<RewindDividerSlot marker={marker} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Undo rewind' }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      'Rewind undone — these files were left as they are: link/a.txt, b.txt'))
  })

  it('says why an undo was refused', async () => {
    vi.spyOn(api, 'post').mockRejectedValue(new ApiRequestError(409, { detail: { code: 'REWIND_UNDO_EXPIRED', message: 'x' } }))
    host(<RewindDividerSlot marker={marker} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Undo rewind' }))
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(
      "This rewind can't be undone any more — a message was sent since"))
  })

  it('has no Undo where the session cannot rewind', async () => {
    host(<RewindDividerSlot marker={{ rewindId: 'r1', filesRestored: ['a'] }} />, { enabled: false })
    expect(await screen.findByText('Rewound — 1 file restored')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Undo rewind' })).toBeNull()
  })
})

describe('useRewind(sessionId).start — the export Rewind & retry reuses', () => {
  beforeEach(() => { vi.restoreAllMocks(); vi.clearAllMocks() })

  function Harness({ onReady }: { onReady: (r: ReturnType<typeof useRewind>) => void }) {
    const r = useRewind('s1')
    useEffect(() => onReady(r))
    return <>{r.element}</>
  }

  it('resolves with the rewind once confirmed, and with null when cancelled', async () => {
    mockApi()
    let rewind: ReturnType<typeof useRewind> | null = null
    host(<Harness onReady={(r) => { rewind = r }} />)
    let done: Promise<RewindResult | null> = Promise.resolve(null)
    act(() => { done = rewind!.start(prompt, { edit: false }) })
    await screen.findByText('src/a.ts')
    await userEvent.click(screen.getByRole('button', { name: 'Rewind' }))
    await expect(done).resolves.toEqual(RESULT)

    act(() => { done = rewind!.start(prompt, { edit: false }) })
    await screen.findByText('src/a.ts')
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await expect(done).resolves.toBeNull()
  })

  it('resolves null at once for a turn with no target', async () => {
    const { result } = renderHook(() => useRewind('s1'), {
      wrapper: ({ children }) => <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>,
    })
    await expect(result.current.start({ id: 'a', role: 'assistant', blocks: [] }, { edit: false })).resolves.toBeNull()
  })
})
