import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('@/api/client', () => ({
  ApiRequestError: class extends Error {},
  api: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn().mockResolvedValue({}),
    patch: vi.fn(),
    del: vi.fn().mockResolvedValue({}),
  },
}))

import { SnapshotsSection } from './SnapshotsSection'
import { api } from '@/api/client'

function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <SnapshotsSection />
    </QueryClientProvider>,
  )
}

const snapshots = [
  {
    id: 's1',
    label: 'Before refactor',
    kind: 'manual',
    createdAt: '2026-07-16T10:00:00.000Z',
    fileCount: 12,
    sizeBytes: 34567,
  },
  {
    id: 's2',
    label: null,
    kind: 'auto',
    createdAt: '2026-07-15T09:00:00.000Z',
    fileCount: 1,
    sizeBytes: 512,
  },
]

beforeEach(() => {
  vi.mocked(api.post).mockClear()
  vi.mocked(api.get).mockClear()
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path === '/snapshots') return Promise.resolve(snapshots)
    if (path === '/snapshots/s1/diff') {
      return Promise.resolve({ added: ['a.md', 'b.md'], removed: ['c.md'], modified: ['d.md'] })
    }
    return Promise.resolve({})
  })
  vi.mocked(api.post).mockResolvedValue({
    id: 'new',
    label: null,
    kind: 'manual',
    createdAt: '2026-07-16T11:00:00.000Z',
    fileCount: 12,
    sizeBytes: 40000,
  })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('SnapshotsSection', () => {
  it('renders the snapshot list from the mocked GET', async () => {
    renderSection()
    expect(await screen.findByText('Before refactor')).toBeTruthy()
    expect(await screen.findByText('(unlabeled)')).toBeTruthy()
    // kind badge + file/size summary
    expect(screen.getByText('Manual')).toBeTruthy()
    expect(screen.getByText('Auto')).toBeTruthy()
    expect(screen.getByText(/12 files/)).toBeTruthy()
  })

  it('"Snapshot now" calls api.post with the trimmed label', async () => {
    renderSection()
    await screen.findByText('Before refactor')

    const labelInput = screen.getByPlaceholderText('Before big refactor')
    await userEvent.type(labelInput, '  my label  ')

    await userEvent.click(screen.getByRole('button', { name: /snapshot now/i }))

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/snapshots', { label: 'my label' })
    })
  })

  it('"Diff" calls the diff endpoint and renders counts', async () => {
    renderSection()
    await screen.findByText('Before refactor')

    const diffBtns = screen.getAllByRole('button', { name: /^diff$/i })
    await userEvent.click(diffBtns[0])

    await waitFor(() => {
      expect(api.get).toHaveBeenCalledWith('/snapshots/s1/diff')
    })
    expect(await screen.findByText('+2 added')).toBeTruthy()
    expect(screen.getByText('−1 removed')).toBeTruthy()
    expect(screen.getByText('~1 modified')).toBeTruthy()

    // Expandable file list
    await userEvent.click(screen.getByRole('button', { name: /show files/i }))
    expect(await screen.findByText('a.md')).toBeTruthy()
  })

  it('"Restore" calls the restore endpoint after confirm', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api.post).mockResolvedValue({ restored: 5, backupId: 'bk-1' })

    renderSection()
    await screen.findByText('Before refactor')

    const restoreBtns = screen.getAllByRole('button', { name: /^restore$/i })
    await userEvent.click(restoreBtns[0])

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/snapshots/s1/restore')
    })
    expect(await screen.findByText(/Restored 5 files/)).toBeTruthy()
  })

  it('"Restore" does nothing when confirm is cancelled', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)

    renderSection()
    await screen.findByText('Before refactor')

    const restoreBtns = screen.getAllByRole('button', { name: /^restore$/i })
    await userEvent.click(restoreBtns[0])

    expect(api.post).not.toHaveBeenCalledWith('/snapshots/s1/restore')
  })

  it('import: selecting a file renders the input and posts a preview via fetch', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ creates: ['new.md'], overwrites: ['old.md'], unchanged: [] }),
    })
    vi.stubGlobal('fetch', fetchMock)

    renderSection()
    await screen.findByText('Before refactor')

    const fileInput = screen.getByLabelText(/select an exported bundle/i) as HTMLInputElement
    expect(fileInput).toBeTruthy()

    const file = new File(['bundle-bytes'], 'config.tar.gz', { type: 'application/gzip' })
    await userEvent.upload(fileInput, file)

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/snapshots/import/preview',
        expect.objectContaining({ method: 'POST' }),
      )
    })
    // Preview counts render before any apply
    expect(await screen.findByText('+1 new')).toBeTruthy()
    expect(screen.getByText('~1 overwritten')).toBeTruthy()
    // Apply button appears only after preview
    expect(screen.getByRole('button', { name: /apply import/i })).toBeTruthy()
  })
})
