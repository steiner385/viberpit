import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('@/api/client', () => ({
  ApiRequestError: class extends Error {},
  api: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn().mockResolvedValue({ events: [], warning: '' }),
    patch: vi.fn(),
    del: vi.fn(),
  },
}))

import { HooksSection } from './HooksSection'
import { api } from '@/api/client'

function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <HooksSection />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const hooksResponse = {
  warning: 'Hooks execute shell on the host machine.',
  events: [
    { event: 'PreToolUse', matcher: 'Bash', command: 'echo hi', enabled: true },
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.get).mockResolvedValue(hooksResponse)
  vi.mocked(api.put).mockResolvedValue(hooksResponse)
  vi.mocked(api.post).mockResolvedValue({
    stdout: 'dry-run-output-line',
    stderr: '',
    exitCode: 0,
    timedOut: false,
    command: 'echo hi',
  })
})

describe('HooksSection', () => {
  it('renders the warning banner with the GET warning text', async () => {
    renderSection()
    expect(await screen.findByText(/Hooks execute shell on the host machine\./)).toBeTruthy()
  })

  it('lists a hook returned by GET (event, matcher, command)', async () => {
    renderSection()
    expect(await screen.findByText('PreToolUse')).toBeTruthy()
    expect(await screen.findByText('echo hi')).toBeTruthy()
    expect(screen.getByText('Bash')).toBeTruthy()
  })

  it('Add hook calls api.put with the new row appended', async () => {
    renderSection()
    // wait for GET to resolve so the existing row is in the list
    await screen.findByText('echo hi')

    const eventSelect = screen.getByLabelText('Event') as HTMLSelectElement
    await userEvent.selectOptions(eventSelect, 'Stop')

    const commandInput = screen.getByLabelText('Command')
    await userEvent.type(commandInput, 'notify-send done')

    const addBtn = screen.getByRole('button', { name: /^add$/i })
    await userEvent.click(addBtn)

    await waitFor(() => {
      expect(api.put).toHaveBeenCalledWith('/hooks', {
        events: [
          { event: 'PreToolUse', matcher: 'Bash', command: 'echo hi', enabled: true },
          { event: 'Stop', matcher: null, command: 'notify-send done', enabled: true },
        ],
      })
    })
  })

  it('toggling enable calls api.put with enabled flipped', async () => {
    renderSection()
    await screen.findByText('echo hi')

    const checkbox = screen.getByRole('checkbox', { name: /enable pretooluse hook/i })
    await userEvent.click(checkbox)

    await waitFor(() => {
      expect(api.put).toHaveBeenCalledWith('/hooks', {
        events: [
          { event: 'PreToolUse', matcher: 'Bash', command: 'echo hi', enabled: false },
        ],
      })
    })
  })

  it('dry-run posts the command and shows stdout + exit code', async () => {
    renderSection()
    await screen.findByText('echo hi')

    const dryRunBtn = screen.getByRole('button', { name: /dry run/i })
    await userEvent.click(dryRunBtn)

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/hooks/dry-run', { command: 'echo hi' })
    })

    expect(await screen.findByText('dry-run-output-line')).toBeTruthy()
    expect(await screen.findByText('0')).toBeTruthy()
  })

  it('surfaces a 422 detail inline without losing the form', async () => {
    // Match the mocked ApiRequestError so instanceof works in errorMessage().
    const { ApiRequestError } = await import('@/api/client')
    const err = new ApiRequestError(422, { detail: 'command must not be empty' }) as Error & { body?: Record<string, unknown> }
    // The module mock's stand-in class ignores its constructor args, so set body directly.
    err.body = { detail: 'command must not be empty' }

    vi.mocked(api.put).mockRejectedValueOnce(err)

    renderSection()
    await screen.findByText('echo hi')

    const commandInput = screen.getByLabelText('Command')
    await userEvent.type(commandInput, 'some-command')

    const addBtn = screen.getByRole('button', { name: /^add$/i })
    await userEvent.click(addBtn)

    expect(await screen.findByText('command must not be empty')).toBeTruthy()
    // form input retained
    expect((commandInput as HTMLInputElement).value).toBe('some-command')
  })
})
