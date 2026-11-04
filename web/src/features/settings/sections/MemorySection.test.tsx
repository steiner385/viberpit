/**
 * Settings › Claude config › Memory (D-13): the editor for the shared user
 * memory file. The transport is faked and the real hooks run against it, so the
 * etag the PUT carries is the one the draft was based on.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import MemorySection from './MemorySection'

const COPY = 'Applies to new sessions. Running sessions keep the instructions they started with.'
const PROPAGATION = 'Sessions on other hosts can take up to 10 minutes to see this change'

type Reply = { status: number; body: unknown }
let getReply: Reply
let putReplies: Reply[]
let fetchSpy: ReturnType<typeof vi.spyOn>

function json({ status, body }: Reply) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

beforeEach(() => {
  vi.restoreAllMocks()
  getReply = { status: 200, body: { text: '# Mine\n', etag: 'e1', exists: true } }
  putReplies = []
  fetchSpy = vi.spyOn(global, 'fetch').mockImplementation(async (_url, init) => {
    if ((init?.method ?? 'GET') === 'PUT') return json(putReplies.shift() ?? { status: 500, body: {} })
    return json(getReply)
  })
})

function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><MemorySection /></QueryClientProvider>)
}

function puts(): unknown[] {
  const calls = fetchSpy.mock.calls as [string, RequestInit | undefined][]
  return calls
    .filter(([, init]) => init?.method === 'PUT')
    .map(([url, init]) => ({ url, body: JSON.parse(String(init?.body)) }))
}

const editor = () => screen.getByRole('textbox', { name: 'User memory (CLAUDE.md)' })

describe('MemorySection', () => {
  it('loads the file and shows the propagation copy', async () => {
    renderSection()
    await waitFor(() => expect(editor()).toHaveValue('# Mine\n'))
    expect(fetchSpy).toHaveBeenCalledWith('/api/settings/memory', expect.anything())
    expect(screen.getByText(COPY)).toBeInTheDocument()
    // Nothing to save until the draft differs.
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('saves with the etag it loaded and shows the propagation note', async () => {
    putReplies.push({ status: 200, body: { etag: 'e2', propagation: PROPAGATION } })
    renderSection()
    await waitFor(() => expect(editor()).toHaveValue('# Mine\n'))
    await userEvent.type(editor(), 'be brief')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(PROPAGATION)).toBeInTheDocument()
    expect(puts()).toEqual([{ url: '/api/settings/memory', body: { text: '# Mine\nbe brief', etag: 'e1' } }])

    // The next save is based on the etag the last one returned.
    putReplies.push({ status: 200, body: { etag: 'e3', propagation: PROPAGATION } })
    await userEvent.type(editor(), '!')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(puts()).toHaveLength(2))
    expect(puts()[1]).toEqual({ url: '/api/settings/memory', body: { text: '# Mine\nbe brief!', etag: 'e2' } })
  })

  it('on a conflict shows the current text and keeps the draft until Reload adopts the server copy', async () => {
    putReplies.push({ status: 409, body: { detail: { code: 'MEMORY_CONFLICT', text: '# Theirs\n', etag: 'e9' } } })
    renderSection()
    await waitFor(() => expect(editor()).toHaveValue('# Mine\n'))
    await userEvent.type(editor(), 'draft')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByText('Changed elsewhere')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Current file' })).toHaveTextContent('# Theirs')
    // The draft is not silently lost.
    expect(editor()).toHaveValue('# Mine\ndraft')

    await userEvent.click(screen.getByRole('button', { name: 'Reload' }))
    expect(editor()).toHaveValue('# Theirs\n')
    expect(screen.queryByText('Changed elsewhere')).toBeNull()

    // The next save is based on the server's etag.
    putReplies.push({ status: 200, body: { etag: 'e10', propagation: PROPAGATION } })
    await userEvent.type(editor(), 'x')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(puts()).toHaveLength(2))
    expect(puts()[1]).toEqual({ url: '/api/settings/memory', body: { text: '# Theirs\nx', etag: 'e9' } })
  })

  it('a missing file shows an empty editor and Save creates it', async () => {
    getReply = { status: 200, body: { text: '', etag: '', exists: false } }
    putReplies.push({ status: 200, body: { etag: 'e1', propagation: PROPAGATION } })
    renderSection()
    await waitFor(() => expect(editor()).toHaveValue(''))
    expect(screen.getByText(/No user memory file yet/)).toBeInTheDocument()
    await userEvent.type(editor(), 'hello')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(puts()).toEqual([{ url: '/api/settings/memory', body: { text: 'hello', etag: '' } }]))
    expect(await screen.findByText(PROPAGATION)).toBeInTheDocument()
    expect(screen.queryByText(/No user memory file yet/)).toBeNull()
  })

  it.each([
    [409, 'MEMORY_FILE_IS_LINK', /is a symbolic link/],
    [413, 'MEMORY_TOO_LARGE', /larger than 256 kB/],
    [422, 'MEMORY_NOT_UTF8', /not valid UTF-8/],
  ])('a %s %s read shows a read-only explanation and no editor', async (status, code, copy) => {
    getReply = { status, body: { detail: { code } } }
    renderSection()
    expect(await screen.findByText(copy)).toBeInTheDocument()
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('a refused save keeps the draft and says why', async () => {
    putReplies.push({ status: 413, body: { detail: { code: 'MEMORY_TOO_LARGE' } } })
    renderSection()
    await waitFor(() => expect(editor()).toHaveValue('# Mine\n'))
    await userEvent.type(editor(), 'more')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/larger than 256 kB/)
    expect(editor()).toHaveValue('# Mine\nmore')
  })
})
