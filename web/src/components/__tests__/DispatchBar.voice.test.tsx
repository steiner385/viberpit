import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Mock } from 'vitest'
import { DispatchBar, type DispatchBarProps } from '../DispatchBar'
import { useDictationField } from '@/lib/useDictationField'

vi.mock('@/lib/useDictationField', () => ({ useDictationField: vi.fn() }))
const mockField = useDictationField as unknown as Mock

const baseProps: DispatchBarProps = {
  repos: [],
  reposLoading: false,
  reposError: false,
  onDispatch: vi.fn().mockResolvedValue(undefined),
  onNavigate: vi.fn(),
  onOpenHighlighted: vi.fn(),
  onRowAction: vi.fn(),
  onShowHelp: vi.fn(),
  onEscape: vi.fn(),
  isPending: false,
}

beforeEach(() => { mockField.mockReset() })

describe('DispatchBar voice integration', () => {
  it('shows the mic button when supported and toggles it', async () => {
    const toggle = vi.fn()
    mockField.mockReturnValue({ supported: true, listening: false, error: null, start: vi.fn(), stop: vi.fn(), toggle })
    render(<DispatchBar {...baseProps} />)
    await userEvent.click(screen.getByRole('button', { name: 'Start dictation' }))
    expect(toggle).toHaveBeenCalledTimes(1)
  })

  it('hides the mic button when unsupported', () => {
    mockField.mockReturnValue({ supported: false, listening: false, error: null, start: vi.fn(), stop: vi.fn(), toggle: vi.fn() })
    render(<DispatchBar {...baseProps} />)
    expect(screen.queryByRole('button', { name: 'Start dictation' })).toBeNull()
  })
})
