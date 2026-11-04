/**
 * Settings › Claude config › Memory (D-13): the editor for the shared user
 * memory file. The transport is faked and the real hooks run against it, so the
 * etag the PUT carries is the one the draft was based on.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QK } from '@/api/hooks'
import { queryClient as appQueryClient } from '@/api/queryClient'
import MemorySection from './MemorySection'

// The app's MutationCache toasts a failed mutation unless it opts out; the
// section says every save failure inline, so it must opt out.
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }))
import { toast } from 'sonner'

const COPY = 'Applies to new sessions. Running sessions keep the instructions they started with.'
const PROPAGATION = 'Sessions on other hosts can take up to 10 minutes to see this change'

type Reply = { status: number; body: unknown }
let getReply: Reply
let putReplies: Reply[]
let fetchSpy: ReturnType<typeof vi.spyOn>

function json({ status, body }: Reply) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

afterEach(() => { appQueryClient.clear() })

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(toast.error).mockClear()
  getReply = { status: 200, body: { text: '# Mine\n', etag: 'e1', exists: true } }
  putReplies = []
  fetchSpy = vi.spyOn(global, 'fetch').mockImplementation(async (_url, init) => {
    if ((init?.method ?? 'GET') === 'PUT') return json(putReplies.shift() ?? { status: 500, body: {} })
    return json(getReply)
  })
})

/** In a data router, as on the page: the unsaved-changes guard is a `useBlocker`. */
function renderSection(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  const router = createMemoryRouter([
    { path: '/settings/memory', element: <QueryClientProvider client={qc}><MemorySection /></QueryClientProvider> },
    { path: '/away', element: <p>Away</p> },
  ], { initialEntries: ['/settings/memory'] })
  render(<RouterProvider router={router} />)
  return router
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

  it('Keep my draft adopts the server etag and keeps the draft, so the next save overwrites knowingly', async () => {
    putReplies.push({ status: 409, body: { detail: { code: 'MEMORY_CONFLICT', text: '# Theirs\n', etag: 'e9' } } })
    renderSection()
    await waitFor(() => expect(editor()).toHaveValue('# Mine\n'))
    await userEvent.type(editor(), 'draft')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Keep my draft' }))
    expect(screen.queryByText('Changed elsewhere')).toBeNull()
    expect(editor()).toHaveValue('# Mine\ndraft')
    putReplies.push({ status: 200, body: { etag: 'e10', propagation: PROPAGATION } })
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(puts()).toHaveLength(2))
    expect(puts()[1]).toEqual({ url: '/api/settings/memory', body: { text: '# Mine\ndraft', etag: 'e9' } })
  })

  it('Reload after the file was deleted elsewhere is a missing file again', async () => {
    putReplies.push({ status: 409, body: { detail: { code: 'MEMORY_CONFLICT', text: '', etag: '' } } })
    renderSection()
    await waitFor(() => expect(editor()).toHaveValue('# Mine\n'))
    await userEvent.type(editor(), 'x')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Reload' }))
    expect(editor()).toHaveValue('')
    expect(screen.getByText(/No user memory file yet/)).toBeInTheDocument()
  })

  it('navigating away with a dirty draft asks first, and Keep editing keeps the draft', async () => {
    const router = renderSection()
    await waitFor(() => expect(editor()).toHaveValue('# Mine\n'))
    await userEvent.type(editor(), 'unsaved')
    await act(async () => { await router.navigate('/away') })
    const dialog = await screen.findByRole('dialog', { name: 'Unsaved changes' })
    expect(screen.queryByText('Away')).toBeNull()
    await userEvent.click(within(dialog).getByRole('button', { name: 'Keep editing' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(editor()).toHaveValue('# Mine\nunsaved')

    // Discard and leave goes.
    await act(async () => { await router.navigate('/away') })
    await userEvent.click(await screen.findByRole('button', { name: 'Discard and leave' }))
    expect(await screen.findByText('Away')).toBeInTheDocument()
  })

  it('a clean editor navigates away without asking', async () => {
    const router = renderSection()
    await waitFor(() => expect(editor()).toHaveValue('# Mine\n'))
    await act(async () => { await router.navigate('/away') })
    expect(await screen.findByText('Away')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('seeds the editor from a fresh read, not a cached copy from an earlier visit', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(QK.userMemory, { text: 'stale', etag: 'e0', exists: true })
    putReplies.push({ status: 200, body: { etag: 'e2', propagation: PROPAGATION } })
    renderSection(qc)
    await waitFor(() => expect(editor()).toHaveValue('# Mine\n'))
    await userEvent.type(editor(), '!')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(puts()).toHaveLength(1))
    expect(puts()[0]).toEqual({ url: '/api/settings/memory', body: { text: '# Mine\n!', etag: 'e1' } })
  })

  it.each([
    [409, { code: 'MEMORY_CONFLICT', text: 'x', etag: 'e9' }, 'Changed elsewhere'],
    [413, { code: 'MEMORY_TOO_LARGE' }, /larger than 256 KiB/],
  ])('a %s save on the app query client is said inline, with no toast', async (status, detail, inline) => {
    putReplies.push({ status, body: { detail } })
    renderSection(appQueryClient)
    await waitFor(() => expect(editor()).toHaveValue('# Mine\n'))
    await userEvent.type(editor(), 'y')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(inline)).toBeInTheDocument()
    expect(toast.error).not.toHaveBeenCalled()
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
    [413, 'MEMORY_TOO_LARGE', /larger than 256 KiB/],
    [422, 'MEMORY_NOT_UTF8', /not valid UTF-8/],
  ])('a %s %s read shows a read-only explanation and no editor', async (status, code, copy) => {
    getReply = { status, body: { detail: { code } } }
    renderSection()
    expect(await screen.findByText(copy)).toBeInTheDocument()
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('a failed read is said as a failure, with Retry', async () => {
    getReply = { status: 500, body: { detail: 'boom' } }
    renderSection()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Couldn’t load the user memory file.')
    expect(screen.queryByRole('textbox')).toBeNull()
    getReply = { status: 200, body: { text: 'back', etag: 'e1', exists: true } }
    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(editor()).toHaveValue('back'))
  })

  it('a refused save keeps the draft and says why', async () => {
    putReplies.push({ status: 413, body: { detail: { code: 'MEMORY_TOO_LARGE' } } })
    renderSection()
    await waitFor(() => expect(editor()).toHaveValue('# Mine\n'))
    await userEvent.type(editor(), 'more')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/larger than 256 KiB/)
    expect(editor()).toHaveValue('# Mine\nmore')
  })
})
