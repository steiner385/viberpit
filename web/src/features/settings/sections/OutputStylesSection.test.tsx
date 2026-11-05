/**
 * Settings › Claude config › Output styles (D-16a): the default picker saves
 * `defaultOutputStyle` (the CLI's `default` saves as null), the list shows each
 * style's source, and a user style's file opens read-only. The transport is faked
 * and the real hooks run against it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { queryClient as appQueryClient } from '@/api/queryClient'
import OutputStylesSection from './OutputStylesSection'

vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }))

const STYLES = [
  { name: 'default', source: 'builtin', description: null },
  { name: 'Proactive', source: 'builtin', description: null },
  { name: 'Concise', source: 'builtin', description: null },
  { name: 'Explanatory', source: 'builtin', description: null },
  { name: 'Learning', source: 'builtin', description: null },
  { name: 'Terse', source: 'user', description: 'Short answers only.', path: 'terse.md' },
]

let settings: Record<string, unknown>
let refuse = false
let fetchSpy: ReturnType<typeof vi.spyOn>

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

afterEach(() => { appQueryClient.clear() })

beforeEach(() => {
  vi.restoreAllMocks()
  settings = { defaultOutputStyle: null }
  refuse = false
  fetchSpy = vi.spyOn(global, 'fetch').mockImplementation(async (url, init) => {
    const u = String(url)
    if (u === '/api/settings' && init?.method === 'PUT') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      if (refuse) return json(422, { detail: 'invalid defaultOutputStyle' })
      settings = { ...settings, ...body }
      return json(200, settings)
    }
    if (u === '/api/settings') return json(200, settings)
    if (u === '/api/settings/output-styles') return json(200, { styles: STYLES })
    if (u === '/api/settings/output-styles/terse.md') {
      return json(200, { name: 'Terse', path: 'terse.md', content: '---\nname: Terse\n---\nBe terse.\n' })
    }
    return json(404, { detail: 'not found' })
  })
})

function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([
    { path: '/settings/output-styles', element: <QueryClientProvider client={qc}><OutputStylesSection /></QueryClientProvider> },
  ], { initialEntries: ['/settings/output-styles'] })
  render(<RouterProvider router={router} />)
}

function puts(): unknown[] {
  const calls = fetchSpy.mock.calls as [string, RequestInit | undefined][]
  return calls.filter(([, init]) => init?.method === 'PUT').map(([, init]) => JSON.parse(String(init?.body)))
}

const picker = () => screen.getByRole('combobox', { name: 'Default style' })

describe('OutputStylesSection', () => {
  it('shows the saved default (null = the CLI default) and every style with its source', async () => {
    renderSection()
    await waitFor(() => expect(picker()).toHaveValue('default'))
    expect(within(picker()).getAllByRole('option').map((o) => o.textContent))
      .toEqual(['default', 'Proactive', 'Concise', 'Explanatory', 'Learning', 'Terse'])
    const list = screen.getByRole('list')
    const rows = within(list).getAllByRole('listitem')
    expect(rows).toHaveLength(6)
    expect(rows[0]).toHaveTextContent('Built-in')
    expect(rows[5]).toHaveTextContent('Terse')
    expect(rows[5]).toHaveTextContent('User')
    expect(rows[5]).toHaveTextContent('Short answers only.')
    // Only a user style has a file to view; the copy points at the shared dir.
    expect(within(rows[0]).queryByRole('button', { name: /View/ })).toBeNull()
    expect(screen.getByText('~/.claude/output-styles/')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('saves the chosen default, and the CLI default as null', async () => {
    renderSection()
    await waitFor(() => expect(picker()).toHaveValue('default'))
    await userEvent.selectOptions(picker(), 'Learning')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(puts()).toEqual([{ defaultOutputStyle: 'Learning' }]))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled())

    await userEvent.selectOptions(picker(), 'default')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(puts()[1]).toEqual({ defaultOutputStyle: null }))
  })

  it('keeps showing a saved default the edge no longer lists', async () => {
    settings = { defaultOutputStyle: 'Gone' }
    renderSection()
    await waitFor(() => expect(picker()).toHaveValue('Gone'))
    await userEvent.selectOptions(picker(), 'Concise')
    await userEvent.selectOptions(picker(), 'Gone')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it("opens a user style's file read-only", async () => {
    renderSection()
    await userEvent.click(await screen.findByRole('button', { name: 'View Terse' }))
    const dialog = await screen.findByRole('dialog', { name: 'Terse' })
    await waitFor(() => expect(dialog).toHaveTextContent('Be terse.'))
    expect(dialog).toHaveTextContent('~/.claude/output-styles/terse.md')
    expect(within(dialog).queryByRole('textbox')).toBeNull()
    await userEvent.click(within(dialog).getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('a refused save is said inline and keeps the choice', async () => {
    refuse = true
    renderSection()
    await waitFor(() => expect(picker()).toHaveValue('default'))
    await userEvent.selectOptions(picker(), 'Terse')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('invalid defaultOutputStyle')).toBeInTheDocument()
    expect(picker()).toHaveValue('Terse')
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  })
})
