import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Mock } from 'vitest'
import { Composer } from '../Composer'
import { useDictationField } from '@/lib/useDictationField'

vi.mock('@/lib/useDictationField', () => ({ useDictationField: vi.fn() }))
const mockField = useDictationField as unknown as Mock

const baseProps = {
  status: { state: 'idle', count: 0 },
  mode: 'default',
  onPrompt: vi.fn(),
  onInterrupt: vi.fn(),
  onSetMode: vi.fn(),
}

beforeEach(() => { mockField.mockReset() })

describe('Composer voice integration', () => {
  it('shows the mic button when dictation is supported and toggles it', async () => {
    const toggle = vi.fn()
    mockField.mockReturnValue({ supported: true, listening: false, error: null, start: vi.fn(), stop: vi.fn(), toggle })
    render(<Composer {...baseProps} />)
    const mic = screen.getByRole('button', { name: 'Start dictation' })
    await userEvent.click(mic)
    expect(toggle).toHaveBeenCalledTimes(1)
  })

  it('hides the mic button when dictation is unsupported', () => {
    mockField.mockReturnValue({ supported: false, listening: false, error: null, start: vi.fn(), stop: vi.fn(), toggle: vi.fn() })
    render(<Composer {...baseProps} />)
    expect(screen.queryByRole('button', { name: 'Start dictation' })).toBeNull()
  })
})
