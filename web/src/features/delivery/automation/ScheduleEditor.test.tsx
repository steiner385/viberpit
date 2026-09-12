import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('@/api/client', () => ({
  ApiRequestError: class ApiRequestError extends Error {
    status: number
    body: Record<string, unknown>
    constructor(status: number, body: Record<string, unknown>) {
      super(`API error ${status}`)
      this.status = status
      this.body = body
    }
  },
  api: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  },
}))

import { ScheduleEditor } from './ScheduleEditor'
import { api } from '@/api/client'

const previewResponse = { nextRuns: ['2026-07-16T08:00:00'], presets: {} }

beforeEach(() => {
  vi.mocked(api.post).mockReset()
  vi.mocked(api.post).mockResolvedValue(previewResponse)
})

describe('ScheduleEditor', () => {
  it('selecting "Daily 8am" fills the cron and triggers a preview call', async () => {
    render(<ScheduleEditor schedule={null} onSave={vi.fn()} />)

    const preset = screen.getByLabelText('Schedule preset')
    await userEvent.selectOptions(preset, 'Daily 8am')

    // Cron input reflects the preset.
    const cronInput = screen.getByLabelText('Cron expression') as HTMLInputElement
    expect(cronInput.value).toBe('0 8 * * *')

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        '/automations/schedule/preview',
        { schedule: { cron: '0 8 * * *' } },
      )
    })
  })

  it('renders the returned next-run time from the preview', async () => {
    render(<ScheduleEditor schedule={{ cron: '0 8 * * *' }} onSave={vi.fn()} />)

    const expected = new Date('2026-07-16T08:00:00').toLocaleString()
    expect(await screen.findByText(expected)).toBeTruthy()
  })

  it('Save calls onSave with the current schedule object', async () => {
    const onSave = vi.fn()
    render(<ScheduleEditor schedule={null} onSave={onSave} />)

    await userEvent.selectOptions(screen.getByLabelText('Schedule preset'), 'Daily 8am')

    // Wait for the preview so Save is enabled (no validation error).
    await waitFor(() => expect(api.post).toHaveBeenCalled())

    await userEvent.click(screen.getByRole('button', { name: /save schedule/i }))
    expect(onSave).toHaveBeenCalledWith({ cron: '0 8 * * *' })
  })

  it('choosing "Always" then Save calls onSave with {}', async () => {
    const onSave = vi.fn()
    render(<ScheduleEditor schedule={{ cron: '0 8 * * *' }} onSave={onSave} />)

    await userEvent.selectOptions(screen.getByLabelText('Schedule preset'), 'Always (no restriction)')
    await waitFor(() => expect(api.post).toHaveBeenCalled())

    await userEvent.click(screen.getByRole('button', { name: /save schedule/i }))
    expect(onSave).toHaveBeenCalledWith({})
  })

  it('shows a 422 validation error inline and disables Save', async () => {
    const { ApiRequestError } = await import('@/api/client')
    vi.mocked(api.post).mockRejectedValue(
      new (ApiRequestError as unknown as new (s: number, b: Record<string, unknown>) => Error)(
        422,
        { detail: 'invalid cron expression' },
      ),
    )

    render(<ScheduleEditor schedule={{ cron: 'not a cron' }} onSave={vi.fn()} />)

    expect(await screen.findByRole('alert')).toHaveTextContent('invalid cron expression')
    expect(screen.getByRole('button', { name: /save schedule/i })).toBeDisabled()
  })

  it('shows "active window only" when only activeHours is set (no cron)', async () => {
    render(
      <ScheduleEditor
        schedule={{ activeHours: { start: '09:00', end: '18:00', days: [0, 1, 2, 3, 4] } }}
        onSave={vi.fn()}
      />,
    )

    expect(await screen.findByText(/active window only/i)).toBeTruthy()
  })
})
